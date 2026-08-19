"""Unit tests for :class:`ConfigDrivenV2DevelopmentAgent`.

Covers the four axes it resolves from a :class:`V2TeamConfig` instead of a
hard-coded per-team constant (language default, tool-agent registry, extra
review clause, conventions map), the config-driven ``_read_repo_code``/
``_detect_tooling`` overrides, and a full mocked run of the inherited
``_run_development_workflow`` proving the base actually drives the v2
pipeline end-to-end from config-sourced data. A parity test at the bottom
proves the base can faithfully hold ``backend_code_v2_team``'s real
``PROFILE``/``ToolAgentKind`` values without touching that team's own
``orchestrator.py`` (still Step 3's job, not this one).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from shared.dev_models.models import Task, TaskStatus, TaskType
from software_engineering_team.shared.phases.execution import ReviewDependencies
from software_engineering_team.shared.stack_profile import StackProfile
from software_engineering_team.shared.v2_models import Phase
from software_engineering_team.shared.v2_orchestrator import ConfigDrivenV2DevelopmentAgent
from software_engineering_team.shared.v2_team_config import V2TeamConfig

from ._v2_config_fixtures import make_stack_profile as _make_stack_profile


def _make_config(
    *,
    stack_profile: StackProfile | None = None,
    tool_agent_kinds: frozenset = frozenset({"security", "testing_qa"}),
    extra_review_clause: str = "",
) -> V2TeamConfig:
    """Build a minimal V2TeamConfig for tests, defaulting to a synthetic StackProfile."""
    return V2TeamConfig(
        stack_profile=stack_profile or _make_stack_profile(),
        tool_agent_kinds=tool_agent_kinds,
        extra_review_clause=extra_review_clause,
    )


class TestConstruction:
    def test_stores_config(self):
        config = _make_config()
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        assert agent.config is config

    def test_rejects_none_config(self):
        with pytest.raises(AssertionError):
            ConfigDrivenV2DevelopmentAgent(MagicMock(), None)


class TestConfigDrivenAxes:
    def test_default_language_reads_through_stack_profile(self):
        config = _make_config(stack_profile=_make_stack_profile(default_language="typescript"))
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        assert agent.default_language == "typescript"

    def test_conventions_for_known_language(self):
        config = _make_config(
            stack_profile=_make_stack_profile(
                conventions_by_language={"java": "JAVA RULES", "_default": "PY RULES"}
            )
        )
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        assert agent.conventions_for("java") == "JAVA RULES"

    def test_conventions_for_unknown_language_falls_back_to_default(self):
        config = _make_config(
            stack_profile=_make_stack_profile(
                conventions_by_language={"java": "JAVA RULES", "_default": "PY RULES"}
            )
        )
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        assert agent.conventions_for("rust") == "PY RULES"

    def test_tool_agent_kinds_reads_through_config(self):
        config = _make_config(tool_agent_kinds=frozenset({"security", "documentation"}))
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        assert agent.tool_agent_kinds == frozenset({"security", "documentation"})

    def test_extra_review_clause_reads_through_config(self):
        config = _make_config(extra_review_clause="Also verify accessibility.")
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        assert agent.extra_review_clause == "Also verify accessibility."

    def test_extra_review_clause_defaults_empty(self):
        config = _make_config()
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        assert agent.extra_review_clause == ""


class TestValidateToolAgents:
    def test_passes_when_kinds_match(self):
        config = _make_config(tool_agent_kinds=frozenset({"security", "testing_qa"}))
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        agent._validate_tool_agents({"security": object(), "testing_qa": object()})

    def test_passes_with_enum_like_keys(self):
        """Keys with a ``.value`` (mirroring the real per-team ``ToolAgentKind``
        ``(str, Enum)`` members) are read via ``.value``, not ``str(kind)``."""

        class _Kind:
            def __init__(self, value: str) -> None:
                self.value = value

        config = _make_config(tool_agent_kinds=frozenset({"security", "testing_qa"}))
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        agent._validate_tool_agents({_Kind("security"): object(), _Kind("testing_qa"): object()})

    def test_passes_with_non_string_value_keys(self):
        """A ``.value`` that isn't already a ``str`` (unlike the real per-team
        ``(str, Enum)`` ``ToolAgentKind`` members) is still normalized via
        ``str()`` before comparison against ``tool_agent_kinds: FrozenSet[str]``."""

        class _IntValuedKind:
            def __init__(self, value: int) -> None:
                self.value = value

        config = _make_config(tool_agent_kinds=frozenset({"1", "2"}))
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        agent._validate_tool_agents({_IntValuedKind(1): object(), _IntValuedKind(2): object()})

    def test_raises_on_missing_kind(self):
        config = _make_config(tool_agent_kinds=frozenset({"security", "testing_qa"}))
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        with pytest.raises(ValueError, match="testing_qa"):
            agent._validate_tool_agents({"security": object()})

    def test_raises_on_extra_kind(self):
        config = _make_config(tool_agent_kinds=frozenset({"security"}))
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        with pytest.raises(ValueError, match="documentation"):
            agent._validate_tool_agents({"security": object(), "documentation": object()})


class TestBuildTaskRequirements:
    def test_no_clause_returns_base_unchanged(self):
        config = _make_config(extra_review_clause="")
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        assert agent.build_task_requirements("Build a CRUD API.") == "Build a CRUD API."

    def test_clause_appended_with_blank_line_separator(self):
        config = _make_config(extra_review_clause="Also verify accessibility.")
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        result = agent.build_task_requirements("Build a CRUD API.")
        assert result == "Build a CRUD API.\n\nAlso verify accessibility."

    def test_clause_used_verbatim_when_base_empty(self):
        config = _make_config(extra_review_clause="Also verify accessibility.")
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        assert agent.build_task_requirements("") == "Also verify accessibility."


class TestConfigDrivenRepoReading:
    def test_read_repo_code_uses_config_stack_profile(self, tmp_path: Path):
        (tmp_path / "x.marker").write_text("marker contents")
        (tmp_path / "y.txt").write_text("excluded extension")
        (tmp_path / "skipme").mkdir()
        (tmp_path / "skipme" / "z.marker").write_text("excluded dir")

        config = _make_config(
            stack_profile=_make_stack_profile(
                repo_extensions=frozenset({".marker"}),
                repo_exclude_dirs=frozenset({"skipme"}),
            )
        )
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        out = agent._read_repo_code(tmp_path)
        assert "x.marker" in out
        assert "marker contents" in out
        assert "y.txt" not in out
        assert "z.marker" not in out

    def test_read_repo_code_respects_max_chars_budget_from_config(self, tmp_path: Path):
        (tmp_path / "a.marker").write_text("x" * 50)
        (tmp_path / "b.marker").write_text("y" * 50)

        config = _make_config(
            stack_profile=_make_stack_profile(
                repo_extensions=frozenset({".marker"}),
                repo_max_chars=10,
            )
        )
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        out = agent._read_repo_code(tmp_path)
        assert "x" * 50 not in out
        assert "y" * 50 not in out

    def test_detect_tooling_uses_config_stack_profile(self, tmp_path: Path):
        config = _make_config(
            stack_profile=_make_stack_profile(detect_tooling=lambda _p: (False, True))
        )
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        assert agent._detect_tooling(tmp_path) == (False, True)

    def test_stack_profile_hook_returns_config_stack_profile(self):
        """``_stack_profile()`` overrides the base's ``getattr(self, "PROFILE",
        None)`` lookup, which would otherwise return None here (this class
        has no class-level PROFILE) and silently empty the deliver phase's
        build_verify_label/lint_agent_type."""
        stack_profile = _make_stack_profile()
        config = _make_config(stack_profile=stack_profile)
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)
        assert agent._stack_profile() is stack_profile


class _FakeWorkflowResult:
    def __init__(self, *, task_id: str) -> None:
        self.task_id = task_id
        self.current_phase = None
        self.failure_reason = None
        self.planning_result = None
        self.execution_result = None
        self.iterations_used = 0
        self.final_files = None
        self.documentation_result = None
        self.deliver_result = None
        self.success = False
        self.summary = ""
        self.needs_followup = False


class _FakeReviewConfig:
    pass


class TestFullPipelineRun:
    """Proves the base actually drives the v2 pipeline end-to-end through a
    ``ConfigDrivenV2DevelopmentAgent`` instance, not just exposing inert
    properties: ``_run_development_workflow`` (inherited unchanged from
    ``BaseV2DevelopmentAgent``) runs successfully with fully mocked
    planning/execution/documentation/deliver callables, and the config's
    ``StackProfile`` drives the (mocked-away) tooling/checkout preflight.
    """

    def test_run_development_workflow_succeeds_with_config_driven_agent(self, tmp_path: Path):
        from types import SimpleNamespace

        config = _make_config(
            stack_profile=_make_stack_profile(detect_tooling=lambda _p: (True, True))
        )
        agent = ConfigDrivenV2DevelopmentAgent(MagicMock(), config)

        task = Task(
            id="t-1",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            title="Task",
            description="Do the thing",
        )

        planning_result = SimpleNamespace(microtasks=[SimpleNamespace(id="mt-1")])
        exec_result = SimpleNamespace(
            files={"app.py": "print('ok')\n"},
            microtasks=[SimpleNamespace(status="completed")],
            summary="implemented",
        )
        doc_result = SimpleNamespace(files={}, summary="documented")
        deliver_result = SimpleNamespace(merged=True, branch_ready=True, summary="delivered")

        captured_deliver_kwargs = {}

        def _run_deliver(**kwargs):
            captured_deliver_kwargs.update(kwargs)
            return deliver_result

        result = agent._run_development_workflow(
            repo_path=tmp_path,
            task=task,
            result_cls=_FakeWorkflowResult,
            team_label="Test",
            deliver_in_progress_status="Delivering...",
            logger=MagicMock(),
            checkout_branch=lambda *_a, **_kw: (True, "checked out"),
            configure_quality_tooling=lambda *_a, **_kw: None,
            detect_tooling=agent._detect_tooling,
            emit_branch_ready_progress=False,
            build_tool_agents=lambda _llm: {},
            git_branch_management_kind="git_branch_management",
            run_planning=lambda **_kwargs: planning_result,
            review_label="Reviewing",
            execution_status_text="Starting...",
            review_deps_cls=ReviewDependencies,
            review_config_cls=_FakeReviewConfig,
            run_execution_with_review_gates=lambda **_kwargs: exec_result,
            documentation_status_text="Documenting...",
            run_documentation_phase=lambda **_kwargs: doc_result,
            run_deliver=_run_deliver,
        )

        assert result.success is True
        assert result.current_phase == Phase.DELIVER
        assert result.final_files == {"app.py": "print('ok')\n"}
        assert result.deliver_result is deliver_result

        # The deliver phase must resolve its labels from the config's
        # StackProfile via _stack_profile() -- not silently default to "" as
        # it did before that hook existed (getattr(self, "PROFILE", None)
        # always returned None for this subclass).
        assert (
            captured_deliver_kwargs["build_verify_label"] == config.stack_profile.build_verify_label
        )
        assert captured_deliver_kwargs["build_verify_label"] != ""
        assert captured_deliver_kwargs["lint_agent_type"] == config.stack_profile.name
        assert captured_deliver_kwargs["lint_agent_type"] != ""


class TestBackendConfigParity:
    """Proves ``ConfigDrivenV2DevelopmentAgent`` can faithfully hold
    ``backend_code_v2_team``'s real config values (mirroring
    ``test_v2_team_config.py::TestBackendParity``) without this base being
    wired into that team's own ``orchestrator.py`` yet."""

    def _build(self) -> ConfigDrivenV2DevelopmentAgent:
        from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
        from software_engineering_team.backend_code_v2_team.phases._profile import PROFILE

        config = V2TeamConfig(
            stack_profile=PROFILE,
            tool_agent_kinds=frozenset(k.value for k in ToolAgentKind),
            extra_review_clause="",
        )
        return ConfigDrivenV2DevelopmentAgent(MagicMock(), config)

    def test_default_language_matches_backend_profile(self):
        assert self._build().default_language == "python"

    def test_tool_agent_kinds_match_backend_enum(self):
        from software_engineering_team.backend_code_v2_team.models import ToolAgentKind

        agent = self._build()
        assert agent.tool_agent_kinds == frozenset(k.value for k in ToolAgentKind)

    def test_no_extra_review_clause_for_backend(self):
        assert self._build().build_task_requirements("Build it.") == "Build it."

    def test_conventions_for_java_matches_backend_profile(self):
        from software_engineering_team.backend_code_v2_team.phases._profile import PROFILE

        assert self._build().conventions_for("java") == PROFILE.conventions_for("java")

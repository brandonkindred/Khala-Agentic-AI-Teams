"""Tests for backend_code_v2_team and frontend_code_v2_team orchestrator helpers."""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from software_engineering_team.codegen_team.orchestrator import CodegenDevelopmentAgent


def test_backend_build_tool_agents():
    from software_engineering_team.codegen_team.models import ToolAgentKind
    from software_engineering_team.codegen_team.orchestrator import _build_backend_tool_agents

    mock_llm = MagicMock()
    agents = _build_backend_tool_agents(mock_llm)
    assert ToolAgentKind.DOCUMENTATION in agents
    assert ToolAgentKind.SECURITY in agents
    assert ToolAgentKind.TESTING_QA in agents
    assert ToolAgentKind.BUILD_SPECIALIST in agents


def test_backend_development_agent_init():
    a = CodegenDevelopmentAgent(llm_client=MagicMock(), stack="backend")
    assert a.llm is not None


def test_backend_development_agent_build_tool_runners():
    a = CodegenDevelopmentAgent(llm_client=MagicMock(), stack="backend")

    class _WithRun:
        def run(self, inp):
            return "ran"

    class _WithExecute:
        def execute(self, inp):
            return "executed"

    class _Bare:
        pass

    runners = a._build_tool_runners(
        {
            "k1": _WithRun(),
            "k2": _WithExecute(),
            "k3": _Bare(),  # filtered out
        }
    )
    assert "k1" in runners
    assert "k2" in runners
    assert "k3" not in runners


def test_fe_build_tool_agents():
    from software_engineering_team.codegen_team.models import ToolAgentKind
    from software_engineering_team.codegen_team.orchestrator import _build_frontend_tool_agents

    mock_llm = MagicMock()
    agents = _build_frontend_tool_agents(mock_llm)
    assert ToolAgentKind.DOCUMENTATION in agents
    assert ToolAgentKind.SECURITY in agents
    assert ToolAgentKind.TESTING_QA in agents
    assert ToolAgentKind.ACCESSIBILITY in agents
    assert ToolAgentKind.PERFORMANCE in agents
    assert ToolAgentKind.UX_USABILITY in agents


class TestAssembleToolAgents:
    """Unit tests for BaseV2DevelopmentAgent._assemble_tool_agents."""

    def test_empty_returns_empty_dict(self):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        assert BaseV2DevelopmentAgent._assemble_tool_agents() == {}

    def test_single_entry(self):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        agent = object()
        out = BaseV2DevelopmentAgent._assemble_tool_agents(("k1", agent))
        assert out == {"k1": agent}

    def test_multiple_entries(self):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        a, b = object(), object()
        out = BaseV2DevelopmentAgent._assemble_tool_agents(("k1", a), ("k2", b))
        assert out == {"k1": a, "k2": b}

    def test_duplicate_kind_last_wins(self):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        first, second = object(), object()
        out = BaseV2DevelopmentAgent._assemble_tool_agents(("k", first), ("k", second))
        assert out == {"k": second}


def test_fe_development_agent_init():
    from software_engineering_team.codegen_team.orchestrator import (
        CodegenDevelopmentAgent,
    )

    a = CodegenDevelopmentAgent(llm_client=MagicMock(), stack="frontend")
    assert a.llm is not None


# ── Incremental repo-context cache threading (#3) ──────────────────────────
# The team lead threads one RepoContextCache per repo into the development
# agent's run_workflow so the N tasks of a job re-read only the files each
# merge touched. These pin the two seams: the dev agent consults the cache when
# one is present, and the team lead builds/reuses the cache per resolved repo.


def _backend_dev_agent():
    return CodegenDevelopmentAgent(llm_client=MagicMock(), stack="backend")


def _fe_dev_agent():
    from software_engineering_team.codegen_team.orchestrator import (
        CodegenDevelopmentAgent,
    )

    return CodegenDevelopmentAgent(llm_client=MagicMock(), stack="frontend")


def test_backend_read_existing_code_uses_threaded_cache(tmp_path: Path):
    """With a cache threaded in, _read_existing_code delegates to it (no fresh walk)."""
    (tmp_path / "x.py").write_text("X = 1")
    a = _backend_dev_agent()
    cache = MagicMock()
    a._repo_context_cache = cache

    out = a._read_existing_code(tmp_path)

    cache.read.assert_called_once_with(tmp_path)
    assert out is cache.read.return_value


def test_backend_read_existing_code_fresh_walk_without_cache(tmp_path: Path):
    """With no cache, _read_existing_code falls back to the fresh budgeted walk."""
    (tmp_path / "x.py").write_text("X = 1")
    a = _backend_dev_agent()
    assert a._repo_context_cache is None
    out = a._read_existing_code(tmp_path)
    assert "x.py" in out


def test_fe_read_existing_code_uses_threaded_cache(tmp_path: Path):
    (tmp_path / "x.ts").write_text("export const x = 1;")
    a = _fe_dev_agent()
    cache = MagicMock()
    a._repo_context_cache = cache

    out = a._read_existing_code(tmp_path)

    cache.read.assert_called_once_with(tmp_path)
    assert out is cache.read.return_value


def test_fe_read_existing_code_fresh_walk_without_cache(tmp_path: Path):
    (tmp_path / "x.ts").write_text("export const x = 1;")
    a = _fe_dev_agent()
    out = a._read_existing_code(tmp_path)
    assert "x.ts" in out


def test_backend_team_lead_repo_context_cache_is_lazy_and_reused(tmp_path: Path):
    """The team lead builds one cache per resolved repo and reuses it across calls."""
    from software_engineering_team.codegen_team.orchestrator import (
        CodegenTeamLead,
    )

    lead = CodegenTeamLead(llm_client=MagicMock(), stack="backend")
    first = lead._repo_context_cache_for(tmp_path)
    second = lead._repo_context_cache_for(tmp_path)
    assert first is second  # same resolved repo → reused, not rebuilt

    other = tmp_path / "other"
    other.mkdir()
    third = lead._repo_context_cache_for(other)
    assert third is not first  # distinct repo → distinct cache


def test_fe_team_lead_repo_context_cache_is_lazy_and_reused(tmp_path: Path):
    from software_engineering_team.codegen_team.orchestrator import (
        CodegenTeamLead,
    )

    lead = CodegenTeamLead(llm_client=MagicMock(), stack="frontend")
    first = lead._repo_context_cache_for(tmp_path)
    second = lead._repo_context_cache_for(tmp_path)
    assert first is second

    other = tmp_path / "other"
    other.mkdir()
    third = lead._repo_context_cache_for(other)
    assert third is not first


def test_fe_detect_tooling_nothing_configured(tmp_path: Path):
    from software_engineering_team.codegen_team.orchestrator import (
        CodegenDevelopmentAgent,
    )

    assert CodegenDevelopmentAgent(MagicMock(), "frontend")._detect_tooling(tmp_path) == (False, False)


def test_fe_detect_tooling_angular_and_vitest(tmp_path: Path):
    from software_engineering_team.codegen_team.orchestrator import (
        CodegenDevelopmentAgent,
    )

    (tmp_path / "angular.json").write_text("{}")
    (tmp_path / "vitest.config.js").write_text("")
    has_lint, has_test = CodegenDevelopmentAgent(MagicMock(), "frontend")._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_fe_detect_tooling_real_npm_test_script(tmp_path: Path):
    from software_engineering_team.codegen_team.orchestrator import (
        CodegenDevelopmentAgent,
    )

    (tmp_path / "eslint.config.js").write_text("")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    has_lint, has_test = CodegenDevelopmentAgent(MagicMock(), "frontend")._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_fe_detect_tooling_rejects_placeholder_and_unparseable(tmp_path: Path):
    """A 'no test'/placeholder script and an unparseable package.json both yield no test."""
    from software_engineering_team.codegen_team.orchestrator import (
        CodegenDevelopmentAgent,
    )

    (tmp_path / "eslint.config.js").write_text("")
    (tmp_path / "package.json").write_text("not json {")
    has_lint, has_test = CodegenDevelopmentAgent(MagicMock(), "frontend")._detect_tooling(tmp_path)
    assert has_lint and not has_test  # unparseable package.json → no test detected


# ---------------------------------------------------------------------------
# CodegenDevelopmentAgent._detect_tooling
# ---------------------------------------------------------------------------


def test_backend_detect_tooling_nothing_configured(tmp_path: Path):
    assert CodegenDevelopmentAgent(MagicMock(), "backend")._detect_tooling(tmp_path) == (
        False,
        False,
    )


def test_backend_detect_tooling_ruff_toml_and_pytest_ini(tmp_path: Path):
    (tmp_path / "ruff.toml").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = CodegenDevelopmentAgent(MagicMock(), "backend")._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_backend_detect_tooling_pyproject_blocks(tmp_path: Path):
    """A pyproject.toml carrying [tool.ruff] + [tool.pytest] blocks satisfies both."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 120\n[tool.pytest.ini_options]\n"
    )
    has_lint, has_test = CodegenDevelopmentAgent(MagicMock(), "backend")._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_backend_detect_tooling_tests_dir_required_for_test(tmp_path: Path):
    """Lint alone (ruff.toml) without a tests dir reports lint=True, test=False."""
    (tmp_path / "ruff.toml").write_text("")
    has_lint, has_test = CodegenDevelopmentAgent(MagicMock(), "backend")._detect_tooling(tmp_path)
    assert has_lint and not has_test


def test_backend_detect_tooling_flake8_satisfies_lint(tmp_path: Path):
    (tmp_path / ".flake8").write_text("[flake8]")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = CodegenDevelopmentAgent(MagicMock(), "backend")._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_backend_detect_tooling_setup_cfg_flake8_section_satisfies_lint(tmp_path: Path):
    """A ``[flake8]`` section in ``setup.cfg`` counts as lint config — a common
    flake8 location the file-name-only ``.flake8`` probe would miss (false
    negative)."""
    (tmp_path / "setup.cfg").write_text("[metadata]\nname=app\n\n[flake8]\nmax-line-length=100\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = CodegenDevelopmentAgent(MagicMock(), "backend")._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_backend_detect_tooling_setup_cfg_without_flake8_section_is_not_lint(tmp_path: Path):
    """A ``setup.cfg`` with no ``[flake8]`` section does not satisfy lint — the
    probe keys off the section header, not the file's mere presence."""
    (tmp_path / "setup.cfg").write_text("[metadata]\nname=app\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = CodegenDevelopmentAgent(MagicMock(), "backend")._detect_tooling(tmp_path)
    assert not has_lint and has_test


def test_backend_detect_tooling_ignores_commented_out_section_headers(tmp_path: Path):
    """A section header inside a comment (``# [tool.ruff]``) is not treated as
    real config — the line-anchored probe skips comment lines, so a
    commented-out block no longer produces a false positive."""
    (tmp_path / "pyproject.toml").write_text(
        '# [tool.ruff] commented out\n#   [flake8] also commented\n[tool.poetry]\nname = "app"\n'
    )
    (tmp_path / "setup.cfg").write_text("# [flake8]\n# max-line-length = 100\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = CodegenDevelopmentAgent(MagicMock(), "backend")._detect_tooling(tmp_path)
    assert not has_lint and has_test


def test_backend_detect_tooling_matches_indented_section_header(tmp_path: Path):
    """A section header with leading whitespace still matches — stripping the
    line before the prefix check keeps real (lightly indented) config working."""
    (tmp_path / "pyproject.toml").write_text("  [tool.ruff]\n  line-length = 120\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = CodegenDevelopmentAgent(MagicMock(), "backend")._detect_tooling(tmp_path)
    assert has_lint and has_test


def _toml_parser_available() -> bool:
    try:
        import tomllib  # noqa: F401  Python 3.11+ stdlib

        return True
    except ModuleNotFoundError:
        try:
            import tomli  # noqa: F401  optional backport

            return True
        except ModuleNotFoundError:
            return False


@pytest.mark.skipif(not _toml_parser_available(), reason="no tomllib/tomli parser available")
def test_backend_detect_tooling_toml_multiline_string_header_not_a_false_positive(tmp_path: Path):
    """A ``[tool.ruff]`` line that lives inside a multi-line string value in
    ``pyproject.toml`` (not a real table) must NOT satisfy the lint pre-flight —
    the ``toml_has_section`` parse sees it is a string, not a table, closing the
    multi-line-string false positive the line-anchored text scan would hit."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "app"\ndescription = """\n[tool.ruff]\n"""\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = CodegenDevelopmentAgent(MagicMock(), "backend")._detect_tooling(tmp_path)
    assert not has_lint and has_test


class TestRunPreflight:
    """Tests for the shared ``BaseV2DevelopmentAgent._run_preflight`` helper.

    No current ``run_workflow`` test drives the dev-agent pre-flight's failure
    paths directly (they only unit-test ``_detect_tooling`` in isolation), so
    these exercise ``_run_preflight`` on its own with fake callables.
    """

    @staticmethod
    def _logger():
        import logging

        return logging.getLogger("test_run_preflight")

    def test_no_branch_skips_checkout(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        checkout_calls = []
        configure_calls = []
        update_calls = []

        result = BaseV2DevelopmentAgent._run_preflight(
            task_id="t1",
            repo_path=tmp_path,
            feature_branch_name=None,
            detect_tooling=lambda _p: (True, True),
            checkout_branch=lambda *a: checkout_calls.append(a) or (True, "ok"),
            configure_quality_tooling=lambda p: configure_calls.append(p),
            update_job=lambda **kw: update_calls.append(kw),
            logger=self._logger(),
        )
        assert result is None
        assert checkout_calls == []
        assert configure_calls == []
        assert update_calls == []

    def test_checkout_failure_returns_message_and_skips_tooling(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        detect_calls = []
        configure_calls = []

        result = BaseV2DevelopmentAgent._run_preflight(
            task_id="t1",
            repo_path=tmp_path,
            feature_branch_name="feature/x",
            detect_tooling=lambda p: detect_calls.append(p) or (True, True),
            checkout_branch=lambda _p, _b: (False, "conflict"),
            configure_quality_tooling=lambda p: configure_calls.append(p),
            update_job=lambda **kw: None,
            logger=self._logger(),
        )
        assert result == "Feature branch checkout failed: conflict"
        assert detect_calls == []
        assert configure_calls == []

    @pytest.mark.parametrize(
        "tooling, expected_missing",
        [
            ((False, True), "linting"),
            ((True, False), "testing"),
            ((False, False), "linting and testing"),
        ],
    )
    def test_missing_tooling_returns_combined_message(
        self, tmp_path: Path, tooling, expected_missing
    ):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        result = BaseV2DevelopmentAgent._run_preflight(
            task_id="t1",
            repo_path=tmp_path,
            feature_branch_name=None,
            detect_tooling=lambda _p: tooling,
            checkout_branch=lambda *a: (True, "ok"),
            configure_quality_tooling=lambda p: None,
            update_job=lambda **kw: None,
            logger=self._logger(),
        )
        assert result == (
            f"Pre-flight check failed: {expected_missing} not configured. "
            "The build process requires linting and testing to be set up before coding tasks begin."
        )

    def test_successful_checkout_configures_tooling(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        configure_calls = []

        result = BaseV2DevelopmentAgent._run_preflight(
            task_id="t1",
            repo_path=tmp_path,
            feature_branch_name="feature/x",
            detect_tooling=lambda _p: (True, True),
            checkout_branch=lambda _p, _b: (True, "checked out"),
            configure_quality_tooling=lambda p: configure_calls.append(p),
            update_job=lambda **kw: None,
            logger=self._logger(),
        )
        assert result is None
        assert configure_calls == [tmp_path]

    def test_emit_branch_ready_progress_true_updates_job(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        update_calls = []

        result = BaseV2DevelopmentAgent._run_preflight(
            task_id="t1",
            repo_path=tmp_path,
            feature_branch_name="feature/x",
            detect_tooling=lambda _p: (True, True),
            checkout_branch=lambda *a: (True, "ok"),
            configure_quality_tooling=lambda p: None,
            update_job=lambda **kw: update_calls.append(kw),
            logger=self._logger(),
            emit_branch_ready_progress=True,
        )
        assert result is None
        assert update_calls == [
            {
                "current_phase": "planning",
                "progress": 4,
                "status_text": "Branch feature/x ready",
            }
        ]

    def test_emit_branch_ready_progress_false_by_default(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        update_calls = []

        result = BaseV2DevelopmentAgent._run_preflight(
            task_id="t1",
            repo_path=tmp_path,
            feature_branch_name="feature/x",
            detect_tooling=lambda _p: (True, True),
            checkout_branch=lambda *a: (True, "ok"),
            configure_quality_tooling=lambda p: None,
            update_job=lambda **kw: update_calls.append(kw),
            logger=self._logger(),
        )
        assert result is None
        assert update_calls == []


class _FakePlanningResult:
    def __init__(self, microtask_count: int = 2):
        self.microtasks = list(range(microtask_count))


class _FakeTask:
    def __init__(
        self,
        task_id: str = "t1",
        title: str = "Do the thing",
        feature_branch_name: str | None = None,
        description: str = "",
    ):
        self.id = task_id
        self.title = title
        self.feature_branch_name = feature_branch_name
        self.description = description


class TestRunPlanningAndBranchSetup:
    """Tests for the shared ``BaseV2DevelopmentAgent._run_planning_and_branch_setup`` helper.

    Mirrors ``TestRunPreflight``'s style: fake callables/agents injected so the
    planning-invocation + job-status-update + feature-branch-creation sequence
    is unit-isolated from either team's ``run_workflow``.
    """

    @staticmethod
    def _logger():
        import logging

        return logging.getLogger("test_run_planning_and_branch_setup")

    def test_planning_failure_short_circuits_without_branch_creation(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        git_agent = MagicMock()

        def _raise_run_planning(**kwargs):
            raise ValueError("boom")

        planning_result, feature_branch_name, failure_reason = (
            BaseV2DevelopmentAgent._run_planning_and_branch_setup(
                task_id="t1",
                task=_FakeTask(),
                repo_path=tmp_path,
                architecture=None,
                existing_code="",
                tool_agents={},
                git_agent=git_agent,
                feature_branch_name=None,
                llm=MagicMock(),
                run_planning=_raise_run_planning,
                update_job=lambda **kw: None,
                logger=self._logger(),
            )
        )
        assert planning_result is None
        assert feature_branch_name is None
        assert failure_reason == "Planning failed: boom"
        git_agent.create_feature_branch.assert_not_called()

    def test_planning_returns_none_short_circuits_without_branch_creation(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        git_agent = MagicMock()

        planning_result, feature_branch_name, failure_reason = (
            BaseV2DevelopmentAgent._run_planning_and_branch_setup(
                task_id="t1",
                task=_FakeTask(),
                repo_path=tmp_path,
                architecture=None,
                existing_code="",
                tool_agents={},
                git_agent=git_agent,
                feature_branch_name=None,
                llm=MagicMock(),
                run_planning=lambda **kw: None,
                update_job=lambda **kw: None,
                logger=self._logger(),
            )
        )
        assert planning_result is None
        assert feature_branch_name is None
        assert failure_reason == "Planning returned no result"
        git_agent.create_feature_branch.assert_not_called()

    def test_existing_branch_name_skips_branch_creation(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        git_agent = MagicMock()
        fake_result = _FakePlanningResult()

        planning_result, feature_branch_name, failure_reason = (
            BaseV2DevelopmentAgent._run_planning_and_branch_setup(
                task_id="t1",
                task=_FakeTask(),
                repo_path=tmp_path,
                architecture=None,
                existing_code="",
                tool_agents={},
                git_agent=git_agent,
                feature_branch_name="feature/already-set",
                llm=MagicMock(),
                run_planning=lambda **kw: fake_result,
                update_job=lambda **kw: None,
                logger=self._logger(),
            )
        )
        assert planning_result is fake_result
        assert feature_branch_name == "feature/already-set"
        assert failure_reason is None
        git_agent.create_feature_branch.assert_not_called()

    def test_successful_branch_creation_emits_progress_and_logs(self, tmp_path: Path, caplog):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        git_agent = MagicMock()
        git_agent.create_feature_branch.return_value = (True, "feature/new-branch")
        fake_result = _FakePlanningResult(microtask_count=3)
        update_calls = []

        with caplog.at_level("INFO", logger="test_run_planning_and_branch_setup"):
            planning_result, feature_branch_name, failure_reason = (
                BaseV2DevelopmentAgent._run_planning_and_branch_setup(
                    task_id="t1",
                    task=_FakeTask(),
                    repo_path=tmp_path,
                    architecture=None,
                    existing_code="",
                    tool_agents={},
                    git_agent=git_agent,
                    feature_branch_name=None,
                    llm=MagicMock(),
                    run_planning=lambda **kw: fake_result,
                    update_job=lambda **kw: update_calls.append(kw),
                    logger=self._logger(),
                )
            )

        assert planning_result is fake_result
        assert feature_branch_name == "feature/new-branch"
        assert failure_reason is None
        assert update_calls == [
            {
                "current_phase": "planning",
                "progress": 5,
                "status_text": "Analyzing task and creating implementation plan...",
            },
            {
                "current_phase": "planning",
                "progress": 10,
                "microtasks_total": 3,
                "microtasks_completed": 0,
                "status_text": "Plan created with 3 microtask(s)",
            },
            {
                "current_phase": "planning",
                "progress": 12,
                "status_text": "Creating feature branch...",
            },
            {
                "current_phase": "planning",
                "progress": 14,
                "status_text": "Branch feature/new-branch ready",
            },
        ]
        assert "Created feature branch: feature/new-branch" in caplog.text

    def test_failed_branch_creation_logs_warning_without_failure_reason(
        self, tmp_path: Path, caplog
    ):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        git_agent = MagicMock()
        git_agent.create_feature_branch.return_value = (False, None)
        fake_result = _FakePlanningResult()

        with caplog.at_level("WARNING", logger="test_run_planning_and_branch_setup"):
            planning_result, feature_branch_name, failure_reason = (
                BaseV2DevelopmentAgent._run_planning_and_branch_setup(
                    task_id="t1",
                    task=_FakeTask(),
                    repo_path=tmp_path,
                    architecture=None,
                    existing_code="",
                    tool_agents={},
                    git_agent=git_agent,
                    feature_branch_name=None,
                    llm=MagicMock(),
                    run_planning=lambda **kw: fake_result,
                    update_job=lambda **kw: None,
                    logger=self._logger(),
                )
            )

        assert planning_result is fake_result
        assert feature_branch_name is None
        assert failure_reason is None
        assert "Git agent create_feature_branch failed, deliver will create branch" in caplog.text

    def test_branch_creation_exception_is_swallowed_and_logged(self, tmp_path: Path, caplog):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        git_agent = MagicMock()
        git_agent.create_feature_branch.side_effect = RuntimeError("git exploded")
        fake_result = _FakePlanningResult()

        with caplog.at_level("WARNING", logger="test_run_planning_and_branch_setup"):
            planning_result, feature_branch_name, failure_reason = (
                BaseV2DevelopmentAgent._run_planning_and_branch_setup(
                    task_id="t1",
                    task=_FakeTask(),
                    repo_path=tmp_path,
                    architecture=None,
                    existing_code="",
                    tool_agents={},
                    git_agent=git_agent,
                    feature_branch_name=None,
                    llm=MagicMock(),
                    run_planning=lambda **kw: fake_result,
                    update_job=lambda **kw: None,
                    logger=self._logger(),
                )
            )

        assert planning_result is fake_result
        assert feature_branch_name is None
        assert failure_reason is None
        assert "Git agent create_feature_branch raised: git exploded" in caplog.text

    def test_no_git_agent_skips_branch_creation(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        fake_result = _FakePlanningResult()

        planning_result, feature_branch_name, failure_reason = (
            BaseV2DevelopmentAgent._run_planning_and_branch_setup(
                task_id="t1",
                task=_FakeTask(),
                repo_path=tmp_path,
                architecture=None,
                existing_code="",
                tool_agents={},
                git_agent=None,
                feature_branch_name=None,
                llm=MagicMock(),
                run_planning=lambda **kw: fake_result,
                update_job=lambda **kw: None,
                logger=self._logger(),
            )
        )
        assert planning_result is fake_result
        assert feature_branch_name is None
        assert failure_reason is None


class _FakeExecutionOutput:
    def __init__(self, files=None, summary="impl done"):
        self.files = {} if files is None else files
        self.summary = summary


class TestRunExecutionPhase:
    """Tests for the shared ``BaseV2DevelopmentAgent._run_execution_phase`` helper."""

    @staticmethod
    def _logger():
        import logging

        return logging.getLogger("test_run_execution_phase")

    @staticmethod
    def _call(**overrides):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        result = overrides.pop("result", _FakeWorkflowResult())
        kwargs = {
            "task_id": "t1",
            "task": MagicMock(),
            "planning_result": MagicMock(),
            "repo_path": overrides.pop("repo_path"),
            "architecture": None,
            "spec_content": "",
            "existing_code": "existing",
            "tool_runners": {},
            "progress_callback": lambda *a, **kw: None,
            "review_deps": MagicMock(name="review_deps"),
            "review_config": MagicMock(name="review_config"),
            "llm": MagicMock(),
            "result": result,
            "run_execution_with_review_gates": lambda **kw: _FakeExecutionOutput(
                files={"app.py": "print('ok')"}
            ),
            "update_job": lambda **kw: None,
            "logger": TestRunExecutionPhase._logger(),
            "status_text": "Starting code implementation",
        }
        kwargs.update(overrides)
        out = BaseV2DevelopmentAgent._run_execution_phase(**kwargs)
        return out, result

    def test_success_returns_files_and_sets_phase(self, tmp_path: Path):
        from software_engineering_team.shared.v2_models import Phase

        update_calls = []
        exec_out = _FakeExecutionOutput(files={"app.py": "code"}, summary="done")

        out, result = self._call(
            repo_path=tmp_path,
            update_job=lambda **kw: update_calls.append(kw),
            run_execution_with_review_gates=lambda **kw: exec_out,
        )

        assert out == {"app.py": "code"}
        assert result.current_phase == Phase.EXECUTION
        assert result.execution_result is exec_out
        assert update_calls == [
            {
                "current_phase": "execution",
                "current_microtask": "",
                "progress": 15,
                "status_text": "Starting code implementation",
            }
        ]

    def test_passes_review_deps_config_and_progress_callback_through(self, tmp_path: Path):
        captured = {}

        def _fake_run(**kw):
            captured.update(kw)
            return _FakeExecutionOutput(files={"a.py": "a"})

        review_deps = MagicMock(name="review_deps")
        review_config = MagicMock(name="review_config")
        progress_cb = lambda *a, **kw: None  # noqa: E731

        self._call(
            repo_path=tmp_path,
            review_deps=review_deps,
            review_config=review_config,
            progress_callback=progress_cb,
            run_execution_with_review_gates=_fake_run,
        )

        assert captured["review_deps"] is review_deps
        assert captured["review_config"] is review_config
        assert captured["progress_callback"] is progress_cb

    def test_microtask_review_failed_error_sets_failure_reason(self, tmp_path: Path, caplog):
        from types import SimpleNamespace

        from software_engineering_team.shared.v2_models import MicrotaskReviewFailedError

        mt = SimpleNamespace(id="mt-3")
        review_result = SimpleNamespace(summary="lint failed")
        err = MicrotaskReviewFailedError(mt, review_result)

        def _boom(**kw):
            raise err

        with caplog.at_level("ERROR", logger="test_run_execution_phase"):
            out, result = self._call(repo_path=tmp_path, run_execution_with_review_gates=_boom)

        assert out is None
        assert result.failure_reason == "Microtask mt-3 failed review: lint failed"
        assert "Microtask mt-3 failed review" in caplog.text

    def test_generic_exception_sets_failure_reason(self, tmp_path: Path, caplog):
        def _boom(**kw):
            raise RuntimeError("llm boom")

        with caplog.at_level("ERROR", logger="test_run_execution_phase"):
            out, result = self._call(repo_path=tmp_path, run_execution_with_review_gates=_boom)

        assert out is None
        assert result.failure_reason == "Execution failed: llm boom"
        assert "Execution failed: llm boom" in caplog.text

    def test_empty_files_sets_failure_reason_without_logging(self, tmp_path: Path, caplog):
        with caplog.at_level("ERROR", logger="test_run_execution_phase"):
            out, result = self._call(
                repo_path=tmp_path,
                run_execution_with_review_gates=lambda **kw: _FakeExecutionOutput(files={}),
            )

        assert out is None
        assert result.failure_reason == "Execution produced no files."
        assert result.execution_result is not None  # set before the empty-files check runs
        assert caplog.text == ""

    def test_status_text_is_parameterized_per_team(self, tmp_path: Path):
        update_calls = []
        self._call(
            repo_path=tmp_path,
            status_text="Starting code implementation...",
            update_job=lambda **kw: update_calls.append(kw),
        )
        assert update_calls[0]["status_text"] == "Starting code implementation..."


class _FakeMicrotask:
    def __init__(self, status: str):
        self.status = status


class _FakeExecResult:
    def __init__(self, statuses: list[str]):
        self.microtasks = [_FakeMicrotask(s) for s in statuses]


class _FakeDocResult:
    def __init__(self, files=None, summary="ok"):
        self.files = files or {}
        self.summary = summary


class _FakeDeliverResult:
    def __init__(
        self,
        *,
        merged: bool = False,
        branch_ready: bool = False,
        summary: str = "delivered",
    ):
        self.merged = merged
        self.branch_ready = branch_ready
        self.summary = summary


class _FakeWorkflowResult:
    def __init__(self, task_id: str = ""):
        self.task_id = task_id
        self.iterations_used = 0
        self.current_phase = None
        self.planning_result = None
        self.execution_result = None
        self.documentation_result = None
        self.final_files = None
        self.deliver_result = None
        self.success = False
        self.summary = ""
        self.needs_followup = False
        self.failure_reason = None


class TestRecordExecutionBookkeeping:
    """Tests for ``BaseV2DevelopmentAgent._record_execution_bookkeeping``."""

    @staticmethod
    def _logger():
        import logging

        return logging.getLogger("test_record_execution_bookkeeping")

    def test_counts_sets_iterations_and_commits(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        git_agent = MagicMock()
        result = _FakeWorkflowResult()
        exec_result = _FakeExecResult(["completed", "completed", "review_failed", "pending"])

        completed_count, failed_count = BaseV2DevelopmentAgent._record_execution_bookkeeping(
            task_id="t1",
            result=result,
            exec_result=exec_result,
            repo_path=tmp_path,
            feature_branch_name="feature/t1",
            git_agent=git_agent,
            logger=self._logger(),
        )

        assert completed_count == 2
        assert failed_count == 1
        assert result.iterations_used == 2
        git_agent.commit_current_changes.assert_called_once_with(
            tmp_path, "feat: 2 microtasks completed"
        )

    def test_skips_commit_without_branch_or_method(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        result = _FakeWorkflowResult()
        exec_result = _FakeExecResult(["completed"])

        completed_count, failed_count = BaseV2DevelopmentAgent._record_execution_bookkeeping(
            task_id="t1",
            result=result,
            exec_result=exec_result,
            repo_path=tmp_path,
            feature_branch_name=None,
            git_agent=MagicMock(),
            logger=self._logger(),
        )
        assert (completed_count, failed_count) == (1, 0)
        assert result.iterations_used == 1

        class _NoCommitAgent:
            pass

        result2 = _FakeWorkflowResult()
        BaseV2DevelopmentAgent._record_execution_bookkeeping(
            task_id="t1",
            result=result2,
            exec_result=exec_result,
            repo_path=tmp_path,
            feature_branch_name="feature/t1",
            git_agent=_NoCommitAgent(),
            logger=self._logger(),
        )
        assert result2.iterations_used == 1

    def test_commit_exception_is_logged_and_swallowed(self, tmp_path: Path, caplog):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        git_agent = MagicMock()
        git_agent.commit_current_changes.side_effect = RuntimeError("git boom")
        result = _FakeWorkflowResult()
        exec_result = _FakeExecResult(["completed", "review_failed"])

        with caplog.at_level("WARNING", logger="test_record_execution_bookkeeping"):
            completed_count, failed_count = BaseV2DevelopmentAgent._record_execution_bookkeeping(
                task_id="t1",
                result=result,
                exec_result=exec_result,
                repo_path=tmp_path,
                feature_branch_name="feature/t1",
                git_agent=git_agent,
                logger=self._logger(),
            )

        assert (completed_count, failed_count) == (1, 1)
        assert result.iterations_used == 1
        assert any("Git agent commit_current_changes raised" in r.message for r in caplog.records)


class TestRunDocumentationPhase:
    """Tests for the shared ``BaseV2DevelopmentAgent._run_documentation_phase`` helper.

    Mirrors ``TestRunPreflight`` / ``TestRunPlanningAndBranchSetup``: fake
    callables injected so the documentation status update + phase invocation +
    file-merge + exception-swallow sequence is unit-isolated from either team's
    ``run_workflow``.
    """

    @staticmethod
    def _logger():
        import logging

        return logging.getLogger("test_run_documentation_phase")

    def test_success_merges_files_and_updates_job(self, tmp_path: Path):
        from software_engineering_team.shared.v2_models import Phase
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        update_calls = []
        result = _FakeWorkflowResult()
        current_files = {"a.py": "a"}
        doc = _FakeDocResult(files={"docs/readme.md": "# hi"}, summary="docs done")

        out = BaseV2DevelopmentAgent._run_documentation_phase(
            task_id="t1",
            task=MagicMock(),
            repo_path=tmp_path,
            llm=MagicMock(),
            exec_result=MagicMock(),
            planning_result=MagicMock(),
            tool_agents={},
            result=result,
            current_files=current_files,
            run_documentation_phase=lambda **kw: doc,
            update_job=lambda **kw: update_calls.append(kw),
            logger=self._logger(),
            status_text="Generating documentation and API specs",
        )

        assert result.current_phase == Phase.DOCUMENTATION
        assert result.documentation_result is doc
        assert out == {"a.py": "a", "docs/readme.md": "# hi"}
        assert result.final_files == out
        assert update_calls == [
            {
                "current_phase": "documentation",
                "progress": 80,
                "status_text": "Generating documentation and API specs",
            }
        ]

    def test_success_without_files_leaves_current_files_unchanged(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        result = _FakeWorkflowResult()
        current_files = {"a.py": "a"}
        doc = _FakeDocResult(files={}, summary="nothing")

        out = BaseV2DevelopmentAgent._run_documentation_phase(
            task_id="t1",
            task=MagicMock(),
            repo_path=tmp_path,
            llm=MagicMock(),
            exec_result=MagicMock(),
            planning_result=MagicMock(),
            tool_agents={},
            result=result,
            current_files=current_files,
            run_documentation_phase=lambda **kw: doc,
            update_job=lambda **kw: None,
            logger=self._logger(),
            status_text="Generating documentation and API docs...",
        )

        assert result.documentation_result is doc
        assert out is current_files
        assert out == {"a.py": "a"}
        assert result.final_files is None

    def test_exception_is_swallowed_and_logged(self, tmp_path: Path, caplog):
        from software_engineering_team.shared.v2_models import Phase
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        result = _FakeWorkflowResult()
        current_files = {"a.py": "a"}

        def _raise(**kwargs):
            raise RuntimeError("doc boom")

        with caplog.at_level("WARNING", logger="test_run_documentation_phase"):
            out = BaseV2DevelopmentAgent._run_documentation_phase(
                task_id="t1",
                task=MagicMock(),
                repo_path=tmp_path,
                llm=MagicMock(),
                exec_result=MagicMock(),
                planning_result=MagicMock(),
                tool_agents={},
                result=result,
                current_files=current_files,
                run_documentation_phase=_raise,
                update_job=lambda **kw: None,
                logger=self._logger(),
                status_text="Generating documentation and API specs",
            )

        assert result.current_phase == Phase.DOCUMENTATION
        assert result.documentation_result is None
        assert out is current_files
        assert "Documentation phase failed: doc boom" in caplog.text
        assert "Continuing to Deliver phase" in caplog.text


class TestRunDeliverAndFinalize:
    """Tests for the shared ``BaseV2DevelopmentAgent._run_deliver_and_finalize`` helper."""

    @staticmethod
    def _logger():
        import logging

        return logging.getLogger("test_run_deliver_and_finalize")

    @staticmethod
    def _call(**overrides):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        result = overrides.pop("result", _FakeWorkflowResult())
        kwargs = {
            "task_id": "t1",
            "repo_path": overrides.pop("repo_path"),
            "current_files": {"a.py"},
            "exec_summary": "exec done",
            "task_title": "Do the thing",
            "task_description": "desc",
            "tool_agents": {},
            "feature_branch_name": "feature/t1",
            "merge_to_development": True,
            "failed_count": 0,
            "completed_count": 2,
            "start_time": 100.0,
            "result": result,
            "run_deliver": lambda **kw: _FakeDeliverResult(merged=True),
            "update_job": lambda **kw: None,
            "logger": TestRunDeliverAndFinalize._logger(),
            "team_label": "Backend",
            "deliver_in_progress_status": "Committing changes and preparing delivery",
        }
        kwargs.update(overrides)
        failure = BaseV2DevelopmentAgent._run_deliver_and_finalize(**kwargs)
        return failure, result

    def test_success_merge_emits_complete_status(self, tmp_path: Path, monkeypatch):
        import time

        monkeypatch.setattr(time, "monotonic", lambda: 105.0)
        update_calls = []

        failure, result = self._call(
            repo_path=tmp_path,
            update_job=lambda **kw: update_calls.append(kw),
            run_deliver=lambda **kw: _FakeDeliverResult(merged=True, summary="merged ok"),
        )

        assert failure is None
        assert result.success is True
        assert result.summary == "exec done merged ok"
        assert result.needs_followup is False
        assert update_calls[0] == {
            "current_phase": "deliver",
            "progress": 90,
            "status_text": "Committing changes and preparing delivery",
        }
        assert update_calls[-1] == {
            "current_phase": "deliver",
            "progress": 100,
            "status_text": "Backend task complete",
        }

    def test_failed_microtasks_mark_partial_and_followup(self, tmp_path: Path, monkeypatch):
        import time

        monkeypatch.setattr(time, "monotonic", lambda: 110.0)
        update_calls = []

        failure, result = self._call(
            repo_path=tmp_path,
            failed_count=1,
            team_label="Frontend",
            deliver_in_progress_status="Committing changes and preparing delivery...",
            update_job=lambda **kw: update_calls.append(kw),
            run_deliver=lambda **kw: _FakeDeliverResult(merged=True, summary="merged"),
        )

        assert failure is None
        assert result.success is False
        assert result.needs_followup is True
        assert "(1 microtask(s) failed review)" in result.summary
        assert update_calls[0]["status_text"] == "Committing changes and preparing delivery..."
        assert update_calls[-1] == {
            "current_phase": "deliver",
            "progress": 95,
            "status_text": "Frontend task completed with issues",
        }

    def test_branch_ready_path_when_not_merging(self, tmp_path: Path, monkeypatch):
        import time

        monkeypatch.setattr(time, "monotonic", lambda: 102.0)

        failure, result = self._call(
            repo_path=tmp_path,
            merge_to_development=False,
            run_deliver=lambda **kw: _FakeDeliverResult(branch_ready=True, summary="branch ready"),
        )

        assert failure is None
        assert result.success is True
        assert result.summary == "exec done branch ready"

    def test_deliver_exception_returns_failure_reason(self, tmp_path: Path, caplog):
        def _boom(**kwargs):
            raise RuntimeError("push failed")

        with caplog.at_level("ERROR", logger="test_run_deliver_and_finalize"):
            failure, result = self._call(repo_path=tmp_path, run_deliver=_boom)

        assert failure == "Deliver failed: push failed"
        assert result.failure_reason == failure
        assert result.success is False
        assert "Deliver failed: push failed" in caplog.text

    def test_soft_deliver_failure_marks_unsuccessful_without_exception(
        self, tmp_path: Path, monkeypatch
    ):
        import time

        monkeypatch.setattr(time, "monotonic", lambda: 107.0)
        update_calls = []

        failure, result = self._call(
            repo_path=tmp_path,
            update_job=lambda **kw: update_calls.append(kw),
            run_deliver=lambda **kw: _FakeDeliverResult(
                merged=False, branch_ready=False, summary="deliver failed"
            ),
        )

        assert failure is None
        assert result.failure_reason is None
        assert result.success is False
        assert result.summary == "exec done deliver failed"
        assert result.needs_followup is False
        assert update_calls[-1] == {
            "current_phase": "deliver",
            "progress": 95,
            "status_text": "Backend task completed with issues",
        }

    def test_workflow_timing_log_on_success(self, tmp_path: Path, monkeypatch, caplog):
        import time

        monkeypatch.setattr(time, "monotonic", lambda: 112.5)

        with caplog.at_level("INFO", logger="test_run_deliver_and_finalize"):
            failure, _result = self._call(
                repo_path=tmp_path,
                completed_count=3,
                failed_count=0,
                start_time=100.0,
            )

        assert failure is None
        assert (
            "WORKFLOW SUCCEEDED in 12.5s (3 microtasks completed, 0 failed review)" in caplog.text
        )

    def test_forwards_build_verifier_and_linting_params_to_run_deliver(
        self, tmp_path: Path
    ) -> None:
        """Regression: build_verifier/linting_tool_agent (and their labels) must
        reach ``run_deliver`` unchanged -- this is the compensating gate's only
        entry point into the standalone code-v2 deliver path."""
        captured: dict = {}

        def _capture_run_deliver(**kw):
            captured.update(kw)
            return _FakeDeliverResult(merged=True)

        sentinel_verifier = lambda *a, **k: (True, "")  # noqa: E731
        sentinel_linter = object()

        failure, _result = self._call(
            repo_path=tmp_path,
            run_deliver=_capture_run_deliver,
            build_verifier=sentinel_verifier,
            build_verify_label="backend",
            linting_tool_agent=sentinel_linter,
            lint_agent_type="backend",
        )

        assert failure is None
        assert captured["build_verifier"] is sentinel_verifier
        assert captured["build_verify_label"] == "backend"
        assert captured["linting_tool_agent"] is sentinel_linter
        assert captured["lint_agent_type"] == "backend"

    def test_default_build_verifier_and_linting_params_are_none(self, tmp_path: Path) -> None:
        """Regression: omitting build_verifier/linting_tool_agent (the
        swarm-orchestrated path's calling convention) must skip the gate --
        ``run_deliver`` receives ``None``/``""``, matching the pre-fix
        no-gate behavior for callers that don't opt in."""
        captured: dict = {}

        def _capture_run_deliver(**kw):
            captured.update(kw)
            return _FakeDeliverResult(merged=True)

        failure, _result = self._call(repo_path=tmp_path, run_deliver=_capture_run_deliver)

        assert failure is None
        assert captured["build_verifier"] is None
        assert captured["build_verify_label"] == ""
        assert captured["linting_tool_agent"] is None
        assert captured["lint_agent_type"] == ""


class _FakeReviewDeps:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeReviewConfig:
    pass


_GIT_KIND = "git_branch_management"


class TestRunDevelopmentWorkflow:
    """Tests for the shared ``BaseV2DevelopmentAgent._run_development_workflow`` template
    method — the glue that ``CodegenDevelopmentAgent.run_workflow`` delegates to for
    either stack (backend or frontend). Every stack-specific callable/class is
    injected with a fake here, mirroring ``TestRunPreflight`` /
    ``TestRunPlanningAndBranchSetup`` / ``TestRunDeliverAndFinalize``'s style, so the
    full phase sequencing is unit-isolated from either stack's profile module.
    """

    @staticmethod
    def _logger():
        import logging

        return logging.getLogger("test_run_development_workflow")

    @staticmethod
    def _call(**overrides):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        agent = BaseV2DevelopmentAgent.__new__(BaseV2DevelopmentAgent)
        agent.llm = MagicMock()
        agent._repo_context_cache = None
        agent._read_repo_code = lambda repo_path, max_chars=None: "existing code"
        profile = overrides.pop("profile", None)
        if profile is not None:
            agent.PROFILE = profile

        git_agent = overrides.pop("git_agent", MagicMock())
        exec_result = overrides.pop(
            "exec_result",
            _FakeExecResult(["completed", "completed"]),
        )
        exec_result.files = overrides.pop("exec_files", {"a.py": "a"})
        exec_result.summary = overrides.pop("exec_summary", "exec done")
        run_execution_with_review_gates = overrides.pop(
            "run_execution_with_review_gates", lambda **kw: exec_result
        )

        kwargs = {
            "repo_path": overrides.pop("repo_path"),
            "task": _FakeTask(),
            "architecture": None,
            "spec_content": "",
            "qa_agent": None,
            "security_agent": None,
            "code_review_agent": None,
            "build_verifier": None,
            "linting_tool_agent": None,
            "job_updater": overrides.pop("job_updater", None),
            "review_config": None,
            "merge_to_development": True,
            "repo_context_cache": None,
            "result_cls": _FakeWorkflowResult,
            "team_label": "Backend",
            "deliver_in_progress_status": "Committing changes and preparing delivery",
            "logger": TestRunDevelopmentWorkflow._logger(),
            "checkout_branch": lambda *a: (True, "ok"),
            "configure_quality_tooling": lambda p: None,
            "detect_tooling": lambda p: (True, True),
            "emit_branch_ready_progress": False,
            "build_tool_agents": lambda llm: {_GIT_KIND: git_agent},
            "git_branch_management_kind": _GIT_KIND,
            "run_planning": lambda **kw: _FakePlanningResult(),
            "review_label": "Reviewing code",
            "execution_status_text": "Starting code implementation",
            "review_deps_cls": _FakeReviewDeps,
            "review_config_cls": _FakeReviewConfig,
            "run_execution_with_review_gates": run_execution_with_review_gates,
            "documentation_status_text": "Generating documentation and API specs",
            "run_documentation_phase": lambda **kw: _FakeDocResult(summary="docs done"),
            "run_deliver": lambda **kw: _FakeDeliverResult(merged=True, summary="delivered"),
        }
        kwargs.update(overrides)
        return agent._run_development_workflow(**kwargs)

    def test_happy_path_runs_every_phase_and_succeeds(self, tmp_path: Path):
        result = self._call(repo_path=tmp_path)

        assert result.task_id == "t1"
        assert result.failure_reason is None
        assert result.planning_result is not None
        assert result.execution_result is not None
        assert result.documentation_result is not None
        assert result.deliver_result is not None
        assert result.success is True
        assert result.iterations_used == 2

    def test_forwards_profile_labels_and_verifier_to_deliver(self, tmp_path: Path) -> None:
        """Regression: when a concrete subclass sets ``PROFILE`` (e.g.
        ``CodegenDevelopmentAgent`` with ``stack="backend"``), its
        ``build_verify_label``/``name`` must be forwarded to the deliver phase
        alongside the injected build_verifier/linting_tool_agent -- this is how
        the standalone code-v2 endpoints' quality gate gets its labels."""
        captured: dict = {}

        def _capture_run_deliver(**kw):
            captured.update(kw)
            return _FakeDeliverResult(merged=True, summary="delivered")

        sentinel_verifier = lambda *a, **k: (True, "")  # noqa: E731
        sentinel_linter = object()
        profile = types.SimpleNamespace(name="backend", build_verify_label="Backend build")

        self._call(
            repo_path=tmp_path,
            profile=profile,
            build_verifier=sentinel_verifier,
            linting_tool_agent=sentinel_linter,
            run_deliver=_capture_run_deliver,
        )

        assert captured["build_verifier"] is sentinel_verifier
        assert captured["build_verify_label"] == "Backend build"
        assert captured["linting_tool_agent"] is sentinel_linter
        assert captured["lint_agent_type"] == "backend"

    def test_missing_profile_forwards_empty_labels(self, tmp_path: Path) -> None:
        """Regression: a bare ``BaseV2DevelopmentAgent`` with no ``PROFILE``
        attribute (e.g. this unit-test harness) must forward empty labels
        rather than raising -- locks in the ``getattr(self, "PROFILE", None)``
        fallback added by the fix."""
        captured: dict = {}

        def _capture_run_deliver(**kw):
            captured.update(kw)
            return _FakeDeliverResult(merged=True, summary="delivered")

        self._call(repo_path=tmp_path, run_deliver=_capture_run_deliver)

        assert captured["build_verify_label"] == ""
        assert captured["lint_agent_type"] == ""
        assert captured["build_verifier"] is None
        assert captured["linting_tool_agent"] is None

    def test_preflight_failure_short_circuits(self, tmp_path: Path):
        planning_calls = []
        result = self._call(
            repo_path=tmp_path,
            detect_tooling=lambda p: (False, True),
            run_planning=lambda **kw: planning_calls.append(kw) or _FakePlanningResult(),
        )

        assert result.failure_reason is not None
        assert "linting" in result.failure_reason
        assert planning_calls == []
        assert result.deliver_result is None

    def test_planning_failure_short_circuits(self, tmp_path: Path):
        def _raise_run_planning(**kwargs):
            raise ValueError("planning boom")

        result = self._call(repo_path=tmp_path, run_planning=_raise_run_planning)

        assert result.failure_reason == "Planning failed: planning boom"
        assert result.execution_result is None
        assert result.deliver_result is None

    def test_review_failed_exception_sets_failure_reason(self, tmp_path: Path):
        from types import SimpleNamespace

        from software_engineering_team.shared.v2_models import MicrotaskReviewFailedError

        def _raise_review_failed(**kwargs):
            raise MicrotaskReviewFailedError(
                SimpleNamespace(id="mt-1"),
                SimpleNamespace(summary="needs work"),
            )

        result = self._call(
            repo_path=tmp_path,
            run_execution_with_review_gates=_raise_review_failed,
        )

        assert result.failure_reason == "Microtask mt-1 failed review: needs work"
        assert result.deliver_result is None

    def test_generic_execution_exception_sets_failure_reason(self, tmp_path: Path):
        def _raise(**kwargs):
            raise RuntimeError("exec boom")

        result = self._call(repo_path=tmp_path, run_execution_with_review_gates=_raise)

        assert result.failure_reason == "Execution failed: exec boom"
        assert result.deliver_result is None

    def test_empty_execution_files_sets_failure_reason(self, tmp_path: Path):
        result = self._call(repo_path=tmp_path, exec_files={})

        assert result.failure_reason == "Execution produced no files."
        assert result.deliver_result is None

    def test_emit_branch_ready_progress_forwarded_to_preflight(self, tmp_path: Path):
        update_calls = []

        self._call(
            repo_path=tmp_path,
            task=_FakeTask(feature_branch_name="feature/x"),
            emit_branch_ready_progress=True,
            job_updater=lambda **kw: update_calls.append(kw),
        )

        assert any(call.get("status_text") == "Branch feature/x ready" for call in update_calls)

    def test_emit_branch_ready_progress_false_suppresses_it(self, tmp_path: Path):
        update_calls = []

        self._call(
            repo_path=tmp_path,
            task=_FakeTask(feature_branch_name="feature/x"),
            emit_branch_ready_progress=False,
            job_updater=lambda **kw: update_calls.append(kw),
        )

        assert not any(call.get("status_text") == "Branch feature/x ready" for call in update_calls)

    def test_review_label_forwarded_to_progress_callback(self, tmp_path: Path):
        captured_callback = {}

        def _run_exec(**kwargs):
            captured_callback["cb"] = kwargs["progress_callback"]
            exec_result = _FakeExecResult(["completed"])
            exec_result.files = {"a.py": "a"}
            exec_result.summary = "exec done"
            return exec_result

        update_calls = []
        self._call(
            repo_path=tmp_path,
            review_label="Reviewing",
            run_execution_with_review_gates=_run_exec,
            job_updater=lambda **kw: update_calls.append(kw),
        )

        captured_callback["cb"](1, 0, 1, "Do the thing", microtask_phase="review")
        assert any(
            call.get("status_text", "").startswith("Reviewing: Do the thing")
            for call in update_calls
        )

    def test_job_updater_exception_is_swallowed(self, tmp_path: Path, caplog):
        def _raise(**kwargs):
            raise RuntimeError("updater boom")

        with caplog.at_level("DEBUG", logger="test_run_development_workflow"):
            result = self._call(repo_path=tmp_path, job_updater=_raise)

        assert result.success is True
        assert "job_updater failed: updater boom" in caplog.text

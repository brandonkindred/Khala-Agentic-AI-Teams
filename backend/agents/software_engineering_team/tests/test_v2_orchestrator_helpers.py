"""Tests for backend_code_v2_team and frontend_code_v2_team orchestrator helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_be_build_tool_agents():
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.orchestrator import _build_tool_agents

    mock_llm = MagicMock()
    agents = _build_tool_agents(mock_llm)
    assert ToolAgentKind.DOCUMENTATION in agents
    assert ToolAgentKind.SECURITY in agents
    assert ToolAgentKind.TESTING_QA in agents
    assert ToolAgentKind.BUILD_SPECIALIST in agents


def test_be_development_agent_init():
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    a = BackendDevelopmentAgent(llm_client=MagicMock())
    assert a.llm is not None


def test_be_development_agent_build_tool_runners():
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    a = BackendDevelopmentAgent(llm_client=MagicMock())

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


def test_be_development_agent_read_repo_code(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    (tmp_path / "x.py").write_text("print('x')")
    (tmp_path / "y.txt").write_text("plain")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skip.py").write_text("# do not include")
    out = BackendDevelopmentAgent._read_repo_code(tmp_path)
    assert "x.py" in out
    assert "print('x')" in out
    assert "skip.py" not in out


def test_be_development_agent_read_repo_code_empty(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    out = BackendDevelopmentAgent._read_repo_code(tmp_path)
    assert "No code files" in out


def test_be_development_agent_read_repo_code_max_chars(tmp_path: Path):
    """The max_chars budget truncates at a whole-file boundary: a file whose
    chunk would push the running total past max_chars is excluded, so the output
    is bounded by max_chars and never contains a partial file or the tail."""
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    # 20 files of 400 chars each (chunk ~413 incl. the ``--- fN.py ---`` header).
    # With max_chars=1000 only f0 and f1 fit (413 + 413 = 826 <= 1000); f2 would
    # push the running total to 1239 > 1000, so the walk stops at a file boundary.
    for i in range(20):
        (tmp_path / f"f{i}.py").write_text("x" * 400)
    out = BackendDevelopmentAgent._read_repo_code(tmp_path, max_chars=1000)
    # Bounded by the whole-file budget — not the untruncated 20-file total — and
    # non-empty (at least one file fit), proving max_chars is actually applied.
    assert len(out) <= 1000
    assert len(out) > 400
    # The cutoff is at a file boundary: the first two files are present, the
    # third and the last are not.
    assert "f0.py" in out and "f1.py" in out
    assert "f2.py" not in out
    assert "f19.py" not in out


def test_fe_build_tool_agents():
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.orchestrator import _build_tool_agents

    mock_llm = MagicMock()
    agents = _build_tool_agents(mock_llm)
    assert ToolAgentKind.DOCUMENTATION in agents
    assert ToolAgentKind.SECURITY in agents
    assert ToolAgentKind.TESTING_QA in agents
    assert ToolAgentKind.ACCESSIBILITY in agents
    assert ToolAgentKind.PERFORMANCE in agents
    assert ToolAgentKind.UX_USABILITY in agents


def test_fe_development_agent_init():
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendDevelopmentAgent,
    )

    a = FrontendDevelopmentAgent(llm_client=MagicMock())
    assert a.llm is not None


# ── Incremental repo-context cache threading (#3) ──────────────────────────
# The team lead threads one RepoContextCache per repo into the development
# agent's run_workflow so the N tasks of a job re-read only the files each
# merge touched. These pin the two seams: the dev agent consults the cache when
# one is present, and the team lead builds/reuses the cache per resolved repo.


def _be_dev_agent():
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    return BackendDevelopmentAgent(llm_client=MagicMock())


def _fe_dev_agent():
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendDevelopmentAgent,
    )

    return FrontendDevelopmentAgent(llm_client=MagicMock())


def test_be_read_existing_code_uses_threaded_cache(tmp_path: Path):
    """With a cache threaded in, _read_existing_code delegates to it (no fresh walk)."""
    (tmp_path / "x.py").write_text("X = 1")
    a = _be_dev_agent()
    cache = MagicMock()
    a._repo_context_cache = cache

    out = a._read_existing_code(tmp_path)

    cache.read.assert_called_once_with(tmp_path)
    assert out is cache.read.return_value


def test_be_read_existing_code_fresh_walk_without_cache(tmp_path: Path):
    """With no cache, _read_existing_code falls back to the fresh budgeted walk."""
    (tmp_path / "x.py").write_text("X = 1")
    a = _be_dev_agent()
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


def test_be_team_lead_repo_context_cache_is_lazy_and_reused(tmp_path: Path):
    """The team lead builds one cache per resolved repo and reuses it across calls."""
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendCodeV2TeamLead,
    )

    lead = BackendCodeV2TeamLead(llm_client=MagicMock())
    first = lead._repo_context_cache_for(tmp_path)
    second = lead._repo_context_cache_for(tmp_path)
    assert first is second  # same resolved repo → reused, not rebuilt

    other = tmp_path / "other"
    other.mkdir()
    third = lead._repo_context_cache_for(other)
    assert third is not first  # distinct repo → distinct cache


def test_fe_team_lead_repo_context_cache_is_lazy_and_reused(tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendCodeV2TeamLead,
    )

    lead = FrontendCodeV2TeamLead(llm_client=MagicMock())
    first = lead._repo_context_cache_for(tmp_path)
    second = lead._repo_context_cache_for(tmp_path)
    assert first is second

    other = tmp_path / "other"
    other.mkdir()
    third = lead._repo_context_cache_for(other)
    assert third is not first


def test_fe_detect_tooling_nothing_configured(tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendDevelopmentAgent,
    )

    assert FrontendDevelopmentAgent._detect_tooling(tmp_path) == (False, False)


def test_fe_detect_tooling_angular_and_vitest(tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendDevelopmentAgent,
    )

    (tmp_path / "angular.json").write_text("{}")
    (tmp_path / "vitest.config.js").write_text("")
    has_lint, has_test = FrontendDevelopmentAgent._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_fe_detect_tooling_real_npm_test_script(tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendDevelopmentAgent,
    )

    (tmp_path / "eslint.config.js").write_text("")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    has_lint, has_test = FrontendDevelopmentAgent._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_fe_detect_tooling_rejects_placeholder_and_unparseable(tmp_path: Path):
    """A 'no test'/placeholder script and an unparseable package.json both yield no test."""
    from software_engineering_team.frontend_code_v2_team.orchestrator import (
        FrontendDevelopmentAgent,
    )

    (tmp_path / "eslint.config.js").write_text("")
    (tmp_path / "package.json").write_text("not json {")
    has_lint, has_test = FrontendDevelopmentAgent._detect_tooling(tmp_path)
    assert has_lint and not has_test  # unparseable package.json → no test detected


# ---------------------------------------------------------------------------
# BackendDevelopmentAgent._detect_tooling
# ---------------------------------------------------------------------------


def test_be_detect_tooling_nothing_configured(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    assert BackendDevelopmentAgent._detect_tooling(tmp_path) == (False, False)


def test_be_detect_tooling_ruff_toml_and_pytest_ini(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    (tmp_path / "ruff.toml").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = BackendDevelopmentAgent._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_be_detect_tooling_pyproject_blocks(tmp_path: Path):
    """A pyproject.toml carrying [tool.ruff] + [tool.pytest] blocks satisfies both."""
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 120\n[tool.pytest.ini_options]\n"
    )
    has_lint, has_test = BackendDevelopmentAgent._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_be_detect_tooling_tests_dir_required_for_test(tmp_path: Path):
    """Lint alone (ruff.toml) without a tests dir reports lint=True, test=False."""
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    (tmp_path / "ruff.toml").write_text("")
    has_lint, has_test = BackendDevelopmentAgent._detect_tooling(tmp_path)
    assert has_lint and not has_test


def test_be_detect_tooling_flake8_satisfies_lint(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    (tmp_path / ".flake8").write_text("[flake8]")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = BackendDevelopmentAgent._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_be_detect_tooling_setup_cfg_flake8_section_satisfies_lint(tmp_path: Path):
    """A ``[flake8]`` section in ``setup.cfg`` counts as lint config — a common
    flake8 location the file-name-only ``.flake8`` probe would miss (false
    negative)."""
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    (tmp_path / "setup.cfg").write_text("[metadata]\nname=app\n\n[flake8]\nmax-line-length=100\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = BackendDevelopmentAgent._detect_tooling(tmp_path)
    assert has_lint and has_test


def test_be_detect_tooling_setup_cfg_without_flake8_section_is_not_lint(tmp_path: Path):
    """A ``setup.cfg`` with no ``[flake8]`` section does not satisfy lint — the
    probe keys off the section header, not the file's mere presence."""
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    (tmp_path / "setup.cfg").write_text("[metadata]\nname=app\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = BackendDevelopmentAgent._detect_tooling(tmp_path)
    assert not has_lint and has_test


def test_be_detect_tooling_ignores_commented_out_section_headers(tmp_path: Path):
    """A section header inside a comment (``# [tool.ruff]``) is not treated as
    real config — the line-anchored probe skips comment lines, so a
    commented-out block no longer produces a false positive."""
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    (tmp_path / "pyproject.toml").write_text(
        '# [tool.ruff] commented out\n#   [flake8] also commented\n[tool.poetry]\nname = "app"\n'
    )
    (tmp_path / "setup.cfg").write_text("# [flake8]\n# max-line-length = 100\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = BackendDevelopmentAgent._detect_tooling(tmp_path)
    assert not has_lint and has_test


def test_be_detect_tooling_matches_indented_section_header(tmp_path: Path):
    """A section header with leading whitespace still matches — stripping the
    line before the prefix check keeps real (lightly indented) config working."""
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    (tmp_path / "pyproject.toml").write_text("  [tool.ruff]\n  line-length = 120\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = BackendDevelopmentAgent._detect_tooling(tmp_path)
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
def test_be_detect_tooling_toml_multiline_string_header_not_a_false_positive(tmp_path: Path):
    """A ``[tool.ruff]`` line that lives inside a multi-line string value in
    ``pyproject.toml`` (not a real table) must NOT satisfy the lint pre-flight —
    the ``toml_has_section`` parse sees it is a string, not a table, closing the
    multi-line-string false positive the line-anchored text scan would hit."""
    from software_engineering_team.backend_code_v2_team.orchestrator import (
        BackendDevelopmentAgent,
    )

    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "app"\ndescription = """\n[tool.ruff]\n"""\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]")
    has_lint, has_test = BackendDevelopmentAgent._detect_tooling(tmp_path)
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
    def __init__(self, task_id: str = "t1", title: str = "Do the thing"):
        self.id = task_id
        self.title = title


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

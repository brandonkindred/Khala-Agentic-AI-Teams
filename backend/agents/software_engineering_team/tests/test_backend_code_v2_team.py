"""
Unit tests for the backend-code-v2 team: models, phases, tool agents, orchestrator.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from llm_service.clients.dummy import DummyLLMClient  # noqa: E402
from software_engineering_team.tests.test_helpers import (  # noqa: E402
    init_repo_with_existing_development,
)


class _TextStubClient(DummyLLMClient):
    """Returns a canned text response through the Strands ``stream()`` path.

    ``complete_json`` returns a plain string (not dict) so that
    ``DummyLLMClient.stream()`` emits it as raw text, matching the template-based
    output parsers used by the backend-code-v2 phases.
    """

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self._text = text

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Any:
        return self._text


from backend_code_v2_team.models import (  # noqa: E402
    BackendCodeV2WorkflowResult,
    ExecutionResult,
    Microtask,
    MicrotaskStatus,
    Phase,
    PlanningResult,
    ReviewIssue,
    ReviewResult,
    SetupResult,
    ToolAgentInput,
    ToolAgentKind,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)

# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    def test_microtask_defaults(self):
        mt = Microtask(id="mt-1")
        assert mt.status == MicrotaskStatus.PENDING
        assert mt.tool_agent == ""
        assert mt.depends_on == []
        assert mt.output_files == {}

    def test_planning_result_defaults(self):
        pr = PlanningResult()
        assert pr.language == ""
        assert pr.microtasks == []

    def test_workflow_result_defaults(self):
        wr = BackendCodeV2WorkflowResult()
        assert not wr.success
        assert wr.current_phase == Phase.SETUP
        assert wr.iterations_used == 0
        assert wr.setup_result is None

    def test_review_issue_model(self):
        issue = ReviewIssue(
            source="qa",
            severity="high",
            description="Missing error handler",
            file_path="app/main.py",
        )
        assert issue.severity == "high"

    def test_tool_agent_io(self):
        mt = Microtask(id="mt-test", description="test")
        inp = ToolAgentInput(microtask=mt, repo_path="/tmp/repo", language="java")
        assert inp.language == "java"
        out = ToolAgentOutput(files={"a.java": "class A {}"}, summary="done")
        assert out.success

    def test_phase_enum_includes_setup(self):
        assert Phase.SETUP.value == "setup"
        assert Phase.SETUP in Phase

    def test_setup_result_model(self):
        sr = SetupResult(repo_initialized=True, readme_created=True, branch_created=True)
        assert sr.repo_initialized
        assert sr.master_renamed_to_main is False

    def test_tool_agent_phase_input_output(self):
        inp = ToolAgentPhaseInput(phase=Phase.PLANNING, task_title="Build API", language="python")
        assert inp.phase == Phase.PLANNING
        out = ToolAgentPhaseOutput(recommendations=["Add auth"], success=True)
        assert out.success


# ---------------------------------------------------------------------------
# Setup phase tests
# ---------------------------------------------------------------------------


class TestSetupPhase:
    def test_run_setup_on_existing_repo(self, tmp_path):
        """Verify setup on an existing repo stays on development without creating a branch."""
        from backend_code_v2_team.phases._profile import run_setup

        init_repo_with_existing_development(tmp_path)
        result = run_setup(repo_path=tmp_path, task_title="My Project")
        assert isinstance(result, SetupResult)
        assert result.summary is not None
        assert "Setup failed" not in result.summary
        assert result.branch_created is False
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
            text=True,
        )
        assert branch.stdout.strip() == "development"

    def test_run_setup_creates_repo_when_missing(self, tmp_path):
        """Verify setup initializes a new git repository when none exists."""
        from backend_code_v2_team.phases._profile import run_setup

        assert not (tmp_path / ".git").exists()
        result = run_setup(repo_path=tmp_path, task_title="New Project")
        assert result.repo_initialized or (tmp_path / ".git").exists()
        assert result.summary

    def test_run_setup_commits_scaffolding_leaving_clean_tree(self, tmp_path):
        """Setup must commit its lint/test scaffolding so the tree stays clean.

        Uncommitted scaffolding on ``development`` is regenerated as untracked
        files on a later pass and blocks the development agent's checkout of the
        review feature branch.
        """
        from backend_code_v2_team.phases._profile import run_setup

        init_repo_with_existing_development(tmp_path)
        run_setup(repo_path=tmp_path, task_title="My Project")
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
            text=True,
        )
        assert status.stdout.strip() == ""

    def test_revision_branch_checkout_survives_setup_regeneration(self, tmp_path):
        """A feature branch tracking the scaffolding must remain checkout-able.

        Reproduces the rejected-task revision flow: pass 1 leaves the scaffolding
        committed (on development and inherited by the feature branch); a second
        ``run_setup`` on development must not strand untracked copies that abort
        the feature-branch checkout.
        """
        from backend_code_v2_team.phases._profile import run_setup

        init_repo_with_existing_development(tmp_path)
        # Pass 1: configure + commit scaffolding on development.
        run_setup(repo_path=tmp_path, task_title="My Project")
        # Simulate the pass-1 review branch carrying the committed scaffolding.
        subprocess.run(
            ["git", "checkout", "-b", "feature/task-1"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        (tmp_path / "feature_change.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: pass 1"], cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "checkout", "development"], cwd=tmp_path, capture_output=True, check=True
        )
        # Pass 2: setup regenerates nothing (idempotent) and leaves a clean tree.
        run_setup(repo_path=tmp_path, task_title="My Project")
        checkout = subprocess.run(
            ["git", "checkout", "feature/task-1"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert checkout.returncode == 0, checkout.stderr

    def test_setup_does_not_sweep_unrelated_work_into_commit(self, tmp_path):
        """The scaffolding commit must include only what setup wrote.

        Pre-existing uncommitted/untracked work must not be swept onto
        ``development`` under the scaffolding commit.
        """
        from backend_code_v2_team.phases._profile import run_setup

        init_repo_with_existing_development(tmp_path)
        subprocess.run(
            ["git", "checkout", "development"], cwd=tmp_path, capture_output=True, check=True
        )
        # Unrelated work present before setup runs.
        (tmp_path / "unrelated.py").write_text("y = 2\n", encoding="utf-8")
        run_setup(repo_path=tmp_path, task_title="My Project")
        # The unrelated file is still untracked (not committed by setup).
        status = subprocess.run(
            ["git", "status", "--porcelain", "unrelated.py"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
            text=True,
        )
        assert status.stdout.strip() == "?? unrelated.py"
        committed = subprocess.run(
            ["git", "ls-files", "unrelated.py"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
            text=True,
        )
        assert committed.stdout.strip() == ""

    def test_setup_logs_when_scaffolding_commit_fails(self, tmp_path, monkeypatch, caplog):
        """A non-raising commit failure (e.g. a rejecting hook) must be logged.

        Otherwise setup reports success while the scaffolding stays uncommitted,
        silently reintroducing the feature-branch checkout conflict.
        """
        from backend_code_v2_team.phases import _profile as setup_mod

        init_repo_with_existing_development(tmp_path)
        monkeypatch.setattr(setup_mod, "commit_paths", lambda *a, **k: (False, "rejected by hook"))
        with caplog.at_level("WARNING"):
            setup_mod.run_setup(repo_path=tmp_path, task_title="My Project")
        assert "not committed" in caplog.text.lower()

    def test_setup_commits_its_edit_to_already_dirty_config(self, tmp_path):
        """Setup's edit to a pre-existing dirty config file must be committed.

        If pyproject.toml was already dirty before setup, a dirty-delta approach
        would drop setup's appended ruff/pytest config, leaving the file dirty
        and re-blocking the later feature-branch checkout. The committed file
        must be clean afterward.
        """
        from backend_code_v2_team.phases._profile import run_setup

        init_repo_with_existing_development(tmp_path)
        subprocess.run(
            ["git", "checkout", "development"], cwd=tmp_path, capture_output=True, check=True
        )
        # pyproject.toml present and dirty (no ruff config yet) before setup runs.
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
        run_setup(repo_path=tmp_path, task_title="My Project")
        status = subprocess.run(
            ["git", "status", "--porcelain", "pyproject.toml"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
            text=True,
        )
        assert status.stdout.strip() == ""  # setup's edit committed, not left dirty
        content = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert "[tool.ruff]" in content

    def test_configure_quality_tooling_adds_config_to_handoff_branch(self, tmp_path):
        """A feature branch created before setup must get lint/test config on demand.

        Reproduces the coding-team handoff: the adapter creates the review branch
        from development *before* setup commits scaffolding there, so the branch
        lacks the config until the dev-agent calls configure_quality_tooling on
        it. Without that, pre-flight (and later quality gates) fail on a
        config-less branch.
        """
        from backend_code_v2_team.phases._profile import configure_quality_tooling, run_setup

        init_repo_with_existing_development(tmp_path)
        subprocess.run(
            ["git", "checkout", "development"], cwd=tmp_path, capture_output=True, check=True
        )
        # Adapter pre-creates the review branch from development (pre-scaffolding).
        subprocess.run(
            ["git", "checkout", "-b", "feature/task-1"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "development"], cwd=tmp_path, capture_output=True, check=True
        )
        # Setup commits scaffolding to development; the feature branch lacks it.
        run_setup(repo_path=tmp_path, task_title="My Project")
        subprocess.run(
            ["git", "checkout", "feature/task-1"], cwd=tmp_path, capture_output=True, check=True
        )
        assert not (tmp_path / "pyproject.toml").exists()  # branch has no ruff config yet

        lint_ok, test_ok = configure_quality_tooling(tmp_path)

        assert lint_ok and test_ok
        assert "[tool.ruff]" in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert (tmp_path / "tests").is_dir()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
            text=True,
        )
        assert status.stdout.strip() == ""  # config committed to the feature branch, tree clean


class TestSetupPhaseHooks:
    """Direct unit tests for the backend lint/test detection hooks.

    These hooks moved from the (coverage-omitted) former ``phases/setup.py``
    into ``phases/_profile.py`` as part of unifying setup onto shared config;
    ``_profile.py`` is not coverage-omitted, so the "already configured" and
    unreadable-config branches need direct coverage here rather than relying
    on the happy-path exercised via ``run_setup`` above.
    """

    def test_ensure_linting_configured_detects_existing_ruff_toml(self, tmp_path):
        from backend_code_v2_team.phases._profile import _ensure_linting_configured

        (tmp_path / "ruff.toml").write_text("", encoding="utf-8")
        written: set = set()
        assert _ensure_linting_configured(tmp_path, written) is True
        assert written == set()

    def test_ensure_linting_configured_detects_existing_flake8_file(self, tmp_path):
        from backend_code_v2_team.phases._profile import _ensure_linting_configured

        (tmp_path / ".flake8").write_text("", encoding="utf-8")
        written: set = set()
        assert _ensure_linting_configured(tmp_path, written) is True
        assert written == set()

    def test_ensure_linting_configured_detects_setup_cfg_flake8_section(self, tmp_path):
        from backend_code_v2_team.phases._profile import _ensure_linting_configured

        (tmp_path / "setup.cfg").write_text("[flake8]\nmax-line-length = 120\n", encoding="utf-8")
        written: set = set()
        assert _ensure_linting_configured(tmp_path, written) is True
        assert written == set()

    def test_ensure_linting_configured_handles_unreadable_pyproject(self, tmp_path, monkeypatch):
        """A pyproject.toml that raises on its first (existing-config probe)
        read must not raise; setup falls through to appending ruff config."""
        from backend_code_v2_team.phases._profile import _ensure_linting_configured

        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
        original_read_text = Path.read_text
        calls = {"n": 0}

        def flaky_read_text(self, *args, **kwargs):
            if self.name == "pyproject.toml" and calls["n"] == 0:
                calls["n"] += 1
                raise OSError("simulated unreadable pyproject.toml")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", flaky_read_text)
        written: set = set()
        assert _ensure_linting_configured(tmp_path, written) is True
        assert "[tool.ruff]" in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")

    def test_ensure_linting_configured_handles_unreadable_setup_cfg(self, tmp_path):
        from backend_code_v2_team.phases._profile import _ensure_linting_configured

        (tmp_path / "setup.cfg").mkdir()
        written: set = set()
        assert _ensure_linting_configured(tmp_path, written) is True
        assert "pyproject.toml" in written

    def test_ensure_testing_configured_handles_unreadable_pyproject(self, tmp_path, monkeypatch):
        """A pyproject.toml that raises on its first (existing-config probe)
        read must not raise while probing for a pytest config; setup falls
        through to creating one."""
        from backend_code_v2_team.phases._profile import _ensure_testing_configured

        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
        original_read_text = Path.read_text
        calls = {"n": 0}

        def flaky_read_text(self, *args, **kwargs):
            if self.name == "pyproject.toml" and calls["n"] == 0:
                calls["n"] += 1
                raise OSError("simulated unreadable pyproject.toml")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", flaky_read_text)
        written: set = set()
        assert _ensure_testing_configured(tmp_path, written) is True
        assert "tests/test_main.py" in written
        assert "[tool.pytest" in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")

    def test_ensure_testing_configured_writes_pytest_ini_without_pyproject(self, tmp_path):
        """When no pyproject.toml exists at all, testing config falls back to
        a standalone pytest.ini."""
        from backend_code_v2_team.phases._profile import _ensure_testing_configured

        written: set = set()
        assert _ensure_testing_configured(tmp_path, written) is True
        assert (tmp_path / "pytest.ini").exists()
        assert "pytest.ini" in written


# ---------------------------------------------------------------------------
# Planning phase tests
# ---------------------------------------------------------------------------


class TestPlanningPhase:
    def test_language_detection_python(self, tmp_path):
        from backend_code_v2_team.phases._profile import _detect_language

        from shared.dev_models.models import Task, TaskStatus, TaskType

        (tmp_path / "requirements.txt").write_text("flask")
        task = Task(
            id="t1",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            description="build api",
        )
        assert _detect_language(tmp_path, task) == "python"

    def test_language_detection_java(self, tmp_path):
        from backend_code_v2_team.phases._profile import _detect_language

        from shared.dev_models.models import Task, TaskStatus, TaskType

        (tmp_path / "pom.xml").write_text("<project/>")
        task = Task(
            id="t1",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            description="build api",
        )
        assert _detect_language(tmp_path, task) == "java"

    def test_language_detection_from_description(self, tmp_path):
        from backend_code_v2_team.phases._profile import _detect_language

        from shared.dev_models.models import Task, TaskStatus, TaskType

        task = Task(
            id="t1",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            description="Use Spring Boot and Java",
        )
        assert _detect_language(tmp_path, task) == "java"

    def test_parse_planning_output(self):
        from backend_code_v2_team.phases._profile import _parse_planning_output

        raw = {
            "microtasks": [
                {
                    "id": "mt-1",
                    "title": "Create models",
                    "tool_agent": "data_engineering",
                    "description": "define models",
                },
                {
                    "id": "mt-2",
                    "title": "Create API",
                    "tool_agent": "api_openapi",
                    "description": "routes",
                    "depends_on": ["mt-1"],
                },
            ],
            "language": "python",
            "summary": "Plan created",
        }
        result = _parse_planning_output(raw, "python")
        assert len(result.microtasks) == 2
        assert result.microtasks[0].tool_agent == ToolAgentKind.DATA_ENGINEERING
        assert result.microtasks[1].depends_on == ["mt-1"]

    def test_run_planning_fallback(self, tmp_path):
        from backend_code_v2_team.phases._profile import run_planning

        from shared.dev_models.models import Task, TaskStatus, TaskType

        mock_llm = _TextStubClient(
            "## MICROTASKS ##\n## END MICROTASKS ##\n"
            "## LANGUAGE ##\npython\n## END LANGUAGE ##\n"
            "## SUMMARY ##\nempty\n## END SUMMARY ##"
        )
        task = Task(
            id="t1",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            description="build something",
        )
        result = run_planning(llm=mock_llm, task=task, repo_path=tmp_path)
        assert len(result.microtasks) == 1
        assert result.microtasks[0].id == "mt-implement-task"


# ---------------------------------------------------------------------------
# Execution phase tests
# ---------------------------------------------------------------------------


class TestExecutionPhase:
    def test_run_execution_with_tool_runners(self, tmp_path):
        from backend_code_v2_team.phases._profile import run_execution

        from shared.dev_models.models import Task, TaskStatus, TaskType

        mock_llm = MagicMock()
        task = Task(
            id="t1",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            description="build",
        )

        def fake_runner(inp):
            return ToolAgentOutput(files={"models.py": "class User: pass"}, summary="done")

        planning = PlanningResult(
            microtasks=[
                Microtask(
                    id="mt-1", tool_agent=ToolAgentKind.DATA_ENGINEERING, description="models"
                )
            ],
            language="python",
        )
        result = run_execution(
            llm=mock_llm,
            task=task,
            planning_result=planning,
            repo_path=tmp_path,
            tool_runners={ToolAgentKind.DATA_ENGINEERING: fake_runner},
        )
        assert "models.py" in result.files
        assert result.microtasks[0].status == MicrotaskStatus.COMPLETED

    def test_run_execution_general_fallback(self, tmp_path):
        from backend_code_v2_team.phases._profile import run_execution

        from shared.dev_models.models import Task, TaskStatus, TaskType

        mock_llm = _TextStubClient(
            "## FILE app.py ##\nprint('hello')\n## SUMMARY ##\ndone\n## END SUMMARY ##"
        )
        task = Task(
            id="t1",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            description="build",
        )
        planning = PlanningResult(
            microtasks=[
                Microtask(id="mt-gen", tool_agent=ToolAgentKind.GENERAL, description="general task")
            ],
            language="python",
        )
        result = run_execution(
            llm=mock_llm, task=task, planning_result=planning, repo_path=tmp_path
        )
        assert "app.py" in result.files


# ---------------------------------------------------------------------------
# Review phase tests
# ---------------------------------------------------------------------------


class _CriticalCodeReviewStubClient(DummyLLMClient):
    """Returns one critical finding for every chunk-review call; ``run_coordinator``
    calls ``complete_json`` directly (JSON, schema-validated), unlike the old
    template/``Agent``-based fallback ``_TextStubClient`` targets. No QA/security/
    build agent is configured in the tests using this stub, so the code-review
    chunk call is the only ``complete_json`` call this client ever receives."""

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        return {
            "approved": False,
            "issues": [
                {
                    "severity": "critical",
                    "category": "security",
                    "file_path": "app.py",
                    "description": "SQL injection",
                    "suggestion": "Use parameterized queries.",
                }
            ],
            "summary": "critical issue",
            "spec_compliance_notes": "",
        }


class TestReviewPhase:
    def test_review_passes_no_issues(self, tmp_path):
        from backend_code_v2_team.phases._profile import run_review

        from shared.dev_models.models import Task, TaskStatus, TaskType

        # A bare DummyLLMClient's built-in "senior code reviewer" branch already
        # returns {"approved": True, "issues": []} for the coordinator's chunk-review
        # call -- a clean pass with no custom stub needed.
        mock_llm = DummyLLMClient()
        task = Task(
            id="t1",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            description="build",
        )
        exec_result = ExecutionResult(files={"app.py": "print()"}, microtasks=[])
        result = run_review(
            llm=mock_llm, task=task, execution_result=exec_result, repo_path=tmp_path
        )
        assert result.passed
        assert result.build_ok

    def test_review_fails_on_critical_issues(self, tmp_path):
        from backend_code_v2_team.phases._profile import run_review

        from shared.dev_models.models import Task, TaskStatus, TaskType

        mock_llm = _CriticalCodeReviewStubClient()
        task = Task(
            id="t1",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            description="build",
        )
        exec_result = ExecutionResult(files={"app.py": "query(input)"}, microtasks=[])
        result = run_review(
            llm=mock_llm, task=task, execution_result=exec_result, repo_path=tmp_path
        )
        assert not result.passed


# ---------------------------------------------------------------------------
# Problem-solving phase tests
# ---------------------------------------------------------------------------


class TestProblemSolvingPhase:
    def test_no_actionable_issues(self):
        from backend_code_v2_team.phases.problem_solving import run_problem_solving

        from shared.dev_models.models import Task, TaskStatus, TaskType

        mock_llm = MagicMock()
        task = Task(
            id="t1",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            description="build",
        )
        review = ReviewResult(
            passed=False,
            issues=[
                ReviewIssue(source="code_review", severity="info", description="minor style"),
            ],
        )
        result = run_problem_solving(
            llm=mock_llm, task=task, review_result=review, current_files={"a.py": "pass"}
        )
        assert result.resolved

    def test_applies_fixes(self):
        from backend_code_v2_team.phases.problem_solving import run_problem_solving

        from shared.dev_models.models import Task, TaskStatus, TaskType

        mock_llm = _TextStubClient(
            "## FILE a.py ##\nfixed_code()\n"
            "## FIXES_APPLIED ##\n---\nissue: bug\nfix: fixed\n---\n## END FIXES_APPLIED ##\n"
            "## RESOLVED ##\ntrue\n## END RESOLVED ##\n"
            "## SUMMARY ##\nFixed bug\n## END SUMMARY ##"
        )
        task = Task(
            id="t1",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            description="build",
        )
        review = ReviewResult(
            passed=False,
            issues=[
                ReviewIssue(source="code_review", severity="high", description="null pointer"),
            ],
        )
        result = run_problem_solving(
            llm=mock_llm, task=task, review_result=review, current_files={"a.py": "bad_code()"}
        )
        assert result.resolved
        assert result.files["a.py"] == "fixed_code()"


# ---------------------------------------------------------------------------
# Tool agents tests
# ---------------------------------------------------------------------------


class TestToolAgents:
    def test_data_engineering_agent(self):
        from backend_code_v2_team.tool_agents.data_engineering import DataEngineeringToolAgent

        mock_llm = _TextStubClient(
            "## FILE models.py ##\nclass User: pass\n## SUMMARY ##\nschema done\n## END SUMMARY ##"
        )
        agent = DataEngineeringToolAgent(mock_llm)
        inp = ToolAgentInput(
            microtask=Microtask(id="mt-1", description="create schema"), language="python"
        )
        out = agent.run(inp)
        assert "models.py" in out.files

    def test_api_openapi_agent(self):
        from backend_code_v2_team.tool_agents.api_openapi import ApiOpenApiToolAgent

        mock_llm = _TextStubClient(
            "## FILE routes.py ##\nroute()\n## SUMMARY ##\napi done\n## END SUMMARY ##"
        )
        agent = ApiOpenApiToolAgent(mock_llm)
        inp = ToolAgentInput(
            microtask=Microtask(id="mt-1", description="create endpoint"), language="python"
        )
        out = agent.run(inp)
        assert "routes.py" in out.files

    def test_auth_agent(self):
        from backend_code_v2_team.tool_agents.auth import AuthToolAgent

        mock_llm = _TextStubClient(
            "## FILE auth.py ##\ndef login(): pass\n## SUMMARY ##\nauth done\n## END SUMMARY ##"
        )
        agent = AuthToolAgent(mock_llm)
        inp = ToolAgentInput(
            microtask=Microtask(id="mt-1", description="add login"), language="python"
        )
        out = agent.run(inp)
        assert "auth.py" in out.files

    def test_git_branch_management_agent(self, tmp_path):
        from software_engineering_team.shared.tool_agent_git_branch import (
            GitBranchManagementToolAgent,
        )

        agent = GitBranchManagementToolAgent()
        phase_inp = ToolAgentPhaseInput(
            phase=Phase.DELIVER,
            task_id="t1",
            task_title="API",
            task_description="Build API",
            feature_branch_name=None,
        )
        out = agent.plan(phase_inp)
        assert out.recommendations
        assert out.success
        out = agent.review(phase_inp)
        assert out.summary
        out = agent.problem_solve(phase_inp)
        assert out.summary
        exec_out = agent.execute(ToolAgentInput(microtask=Microtask(id="mt-1"), language="python"))
        assert exec_out.summary

        create_ok, branch = agent.create_feature_branch(tmp_path, "t1", "API")
        assert not create_ok and branch is None

        from shared.git.git_utils import initialize_new_repo

        ok, _ = initialize_new_repo(tmp_path)
        assert ok
        create_ok, branch = agent.create_feature_branch(tmp_path, "t1", "API")
        assert create_ok and branch is not None
        assert "feature/" in branch

    def test_git_agent_commit_current_changes(self, tmp_path):
        from shared.git.git_utils import initialize_new_repo
        from software_engineering_team.shared.tool_agent_git_branch import (
            GitBranchManagementToolAgent,
        )

        initialize_new_repo(tmp_path)
        (tmp_path / "foo.txt").write_text("hi")
        agent = GitBranchManagementToolAgent()
        ok, msg = agent.commit_current_changes(tmp_path, "chore: add foo")
        assert ok

    def test_git_agent_deliver_with_feature_branch_name(self, tmp_path):
        from shared.git.git_utils import (
            create_feature_branch,
            initialize_new_repo,
        )
        from software_engineering_team.shared.tool_agent_git_branch import (
            GitBranchManagementToolAgent,
        )

        initialize_new_repo(tmp_path)
        ok, branch = create_feature_branch(tmp_path, "development", "t1-api")
        assert ok and branch
        phase_inp = ToolAgentPhaseInput(
            phase=Phase.DELIVER,
            repo_path=str(tmp_path),
            task_id="t1",
            task_title="API",
            feature_branch_name=branch,
        )
        agent = GitBranchManagementToolAgent()
        out = agent.deliver(phase_inp)
        assert out.success
        assert "Merged" in out.summary

    def test_build_specialist_stub(self):
        from backend_code_v2_team.tool_agents.build_specialist import BuildSpecialistAdapterAgent

        agent = BuildSpecialistAdapterAgent()
        inp = ToolAgentInput(microtask=Microtask(id="mt-1", description="build"), language="python")
        out = agent.run(inp)
        assert out.summary
        phase_inp = ToolAgentPhaseInput(phase=Phase.REVIEW)
        assert agent.plan(phase_inp).summary
        # review() returns issues when build is run; when repo_path is missing it returns a skip summary
        assert agent.review(phase_inp).summary
        assert agent.problem_solve(phase_inp).summary
        assert agent.deliver(phase_inp).summary

    def test_tool_agents_have_plan_review_problem_solve_deliver(self):
        """Tool agents participate in all phases: plan, execute, review, problem_solve, deliver."""
        from backend_code_v2_team.tool_agents.build_specialist import BuildSpecialistAdapterAgent
        from backend_code_v2_team.tool_agents.data_engineering import DataEngineeringToolAgent

        mock_llm = MagicMock()
        data_eng = DataEngineeringToolAgent(mock_llm)
        inp = ToolAgentPhaseInput(
            phase=Phase.PLANNING, task_title="API", task_description="Build API"
        )
        out = data_eng.plan(inp)
        assert out.recommendations
        assert out.success

        build = BuildSpecialistAdapterAgent()
        rev_out = build.review(inp)
        assert rev_out.summary
        ps_out = build.problem_solve(inp)
        assert ps_out.summary
        del_out = build.deliver(inp)
        assert del_out.summary

    def test_data_engineering_execute_via_run(self):
        """run() delegates to execute() for backward compatibility."""
        from backend_code_v2_team.tool_agents.data_engineering import DataEngineeringToolAgent

        mock_llm = _TextStubClient("## FILE x.py ##\ncode\n## SUMMARY ##\ndone\n## END SUMMARY ##")
        agent = DataEngineeringToolAgent(mock_llm)
        inp = ToolAgentInput(
            microtask=Microtask(id="mt-1", description="schema"), language="python"
        )
        out = agent.run(inp)
        assert out.files
        out2 = agent.execute(inp)
        assert out2.files == out.files


# ---------------------------------------------------------------------------
# BackendDevelopmentAgent tests (5-phase cycle)
# ---------------------------------------------------------------------------


class TestBackendDevelopmentAgent:
    def test_read_repo_code(self, tmp_path):
        from backend_code_v2_team.orchestrator import BackendDevelopmentAgent

        (tmp_path / "app.py").write_text("print('hello')")
        (tmp_path / "readme.md").write_text("# Readme")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "skip.py").write_text("# do not include")
        agent = BackendDevelopmentAgent(MagicMock())
        code = agent._read_repo_code(tmp_path)
        assert "app.py" in code
        assert "print('hello')" in code
        assert "skip.py" not in code

    def test_read_repo_code_empty(self, tmp_path):
        from backend_code_v2_team.orchestrator import BackendDevelopmentAgent

        agent = BackendDevelopmentAgent(MagicMock())
        out = agent._read_repo_code(tmp_path)
        assert "No code files" in out

    def test_read_repo_code_max_chars(self, tmp_path):
        """The max_chars budget truncates at a whole-file boundary: a file whose
        chunk would push the running total past max_chars is excluded, so the output
        is bounded by max_chars and never contains a partial file or the tail."""
        from backend_code_v2_team.orchestrator import BackendDevelopmentAgent

        # 20 files of 400 chars each (chunk ~413 incl. the ``--- fN.py ---`` header).
        # With max_chars=1000 only f0 and f1 fit (413 + 413 = 826 <= 1000); f2 would
        # push the running total to 1239 > 1000, so the walk stops at a file boundary.
        for i in range(20):
            (tmp_path / f"f{i}.py").write_text("x" * 400)
        agent = BackendDevelopmentAgent(MagicMock())
        out = agent._read_repo_code(tmp_path, max_chars=1000)
        # Bounded by the whole-file budget — not the untruncated 20-file total — and
        # non-empty (at least one file fit), proving max_chars is actually applied.
        assert len(out) <= 1000
        assert len(out) > 400
        # The cutoff is at a file boundary: the first two files are present, the
        # third and the last are not.
        assert "f0.py" in out and "f1.py" in out
        assert "f2.py" not in out
        assert "f19.py" not in out

    def test_build_tool_runners(self):
        from backend_code_v2_team.orchestrator import (
            BackendDevelopmentAgent,
            _build_tool_agents_impl,
        )

        mock_llm = MagicMock()
        dev = BackendDevelopmentAgent(mock_llm)
        tool_agents = _build_tool_agents_impl(mock_llm)
        runners = dev._build_tool_runners(tool_agents)
        assert ToolAgentKind.DATA_ENGINEERING in runners
        assert ToolAgentKind.API_OPENAPI in runners
        assert ToolAgentKind.AUTH in runners
        assert ToolAgentKind.GIT_BRANCH_MANAGEMENT in tool_agents
        assert ToolAgentKind.BUILD_SPECIALIST in tool_agents
        git_agent = tool_agents[ToolAgentKind.GIT_BRANCH_MANAGEMENT]
        assert hasattr(git_agent, "create_feature_branch")
        assert hasattr(git_agent, "commit_current_changes")
        assert hasattr(git_agent, "deliver")


# ---------------------------------------------------------------------------
# BackendCodeV2TeamLead (Tech Lead: Setup + delegate) tests
# ---------------------------------------------------------------------------


class TestBackendCodeV2TeamLead:
    def test_team_lead_runs_setup_then_delegates(self, tmp_path):
        """BackendCodeV2TeamLead reports a concrete setup-readiness failure."""
        from backend_code_v2_team.orchestrator import BackendCodeV2TeamLead

        from shared.dev_models.models import Task, TaskStatus, TaskType

        mock_llm = MagicMock()
        planning_response = (
            "## MICROTASKS ##\n---\nid: mt-1\ntitle: A\ndescription: a\ntool_agent: general\ndepends_on: \n---\n## END MICROTASKS ##\n"
            "## LANGUAGE ##\npython\n## END LANGUAGE ##\n## SUMMARY ##\nplan\n## END SUMMARY ##"
        )
        execution_response = "## SUMMARY ##\nnothing\n## END SUMMARY ##"
        mock_llm.complete_text.side_effect = [planning_response, execution_response]

        lead = BackendCodeV2TeamLead(mock_llm)
        task = Task(
            id="t-test",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            description="Do something",
        )

        (tmp_path / ".git").mkdir()

        result = lead.run_workflow(repo_path=tmp_path, task=task)
        assert result.setup_result is not None
        assert not result.success
        assert "linting is not configured" in result.failure_reason.lower()

    def test_team_lead_propagates_development_handoff_fields(self, tmp_path, monkeypatch):
        """Team-lead result preserves the inner development handoff fields."""
        from backend_code_v2_team import orchestrator as orch
        from backend_code_v2_team.models import (
            BackendCodeV2WorkflowResult,
            DeliverResult,
            DocumentationPhaseResult,
            Phase,
            SetupResult,
        )

        from shared.dev_models.models import Task, TaskStatus, TaskType

        deliver = DeliverResult(
            branch_name="feature/api",
            branch_ready=True,
            delivered_files=["app.py"],
            summary="handoff ready",
        )
        documentation = DocumentationPhaseResult(summary="docs updated")
        inner = BackendCodeV2WorkflowResult(
            task_id="api",
            success=True,
            current_phase=Phase.DELIVER,
            iterations_used=2,
            documentation_result=documentation,
            deliver_result=deliver,
            final_files={"app.py": "print('ok')\n"},
            summary="implemented and ready",
            failure_reason="",
            needs_followup=True,
        )

        class _DevelopmentAgent:
            def __init__(self, _llm):
                pass

            def run_workflow(self, **_kwargs):
                return inner

        monkeypatch.setattr(
            orch,
            "run_setup",
            lambda **_kwargs: SetupResult(linting_configured=True, testing_configured=True),
        )
        monkeypatch.setattr(orch, "BackendDevelopmentAgent", _DevelopmentAgent)

        task = Task(
            id="api",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            title="API",
            description="Build API",
        )

        result = orch.BackendCodeV2TeamLead(MagicMock()).run_workflow(
            repo_path=tmp_path,
            task=task,
            merge_to_development=False,
        )

        assert result.success is True
        assert result.current_phase == Phase.DELIVER
        assert result.iterations_used == 2
        assert result.deliver_result is deliver
        # The documentation phase output must survive the team-lead overlay (it was
        # previously dropped, leaving callers with None).
        assert result.documentation_result is documentation
        assert result.final_files == {"app.py": "print('ok')\n"}
        assert result.summary == "implemented and ready"
        assert result.needs_followup is True


class TestBackendDevelopmentAgentBranchReuse:
    def test_existing_feature_branch_is_reused_without_recreation(self, tmp_path, monkeypatch):
        """Revision workflows keep the reviewed branch instead of recreating it from development."""
        from backend_code_v2_team import orchestrator as orch
        from backend_code_v2_team.models import (
            DeliverResult,
            DocumentationPhaseResult,
            ExecutionResult,
            PlanningResult,
        )

        from shared.dev_models.models import Task, TaskStatus, TaskType

        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n[tool.pytest.ini_options]\n")
        (tmp_path / "tests").mkdir()

        captured: dict[str, str] = {}
        events: list[str] = []

        class _GitAgent:
            def __init__(self) -> None:
                self.create_called = False

            def create_feature_branch(self, *_args, **_kwargs):
                self.create_called = True
                raise AssertionError("existing review branch must not be recreated")

            def commit_current_changes(self, *_args, **_kwargs):
                return True, "committed"

        git_agent = _GitAgent()

        def _checkout_branch(_repo_path, branch):
            events.append("checkout")
            captured["checkout"] = branch
            return True, "checked out"

        def _read_repo_code(_self, _repo_path):
            events.append("read_repo")
            return "existing branch code"

        def _run_planning(**kwargs):
            events.append("planning")
            captured["existing_code"] = kwargs["existing_code"]
            return PlanningResult(microtasks=[Microtask(id="mt-1")], summary="planned")

        def _run_execution_with_review_gates(**_kwargs):
            return ExecutionResult(
                files={"app.py": "print('ok')\n"},
                microtasks=[Microtask(id="mt-1", status=MicrotaskStatus.COMPLETED)],
                summary="implemented",
            )

        def _run_deliver(**kwargs):
            captured["deliver_branch"] = kwargs["feature_branch_name"]
            return DeliverResult(
                branch_name=kwargs["feature_branch_name"],
                branch_ready=True,
                summary="ready",
            )

        from backend_code_v2_team.phases import _profile as doc_phase

        monkeypatch.setattr(orch, "checkout_branch", _checkout_branch)
        monkeypatch.setattr(
            orch.BackendDevelopmentAgent,
            "_build_and_validate_tool_agents",
            lambda _self, _llm: {ToolAgentKind.GIT_BRANCH_MANAGEMENT: git_agent},
        )
        monkeypatch.setattr(orch.BackendDevelopmentAgent, "_read_repo_code", _read_repo_code)
        monkeypatch.setattr(orch, "run_planning", _run_planning)
        monkeypatch.setattr(
            orch, "run_execution_with_review_gates", _run_execution_with_review_gates
        )
        monkeypatch.setattr(
            doc_phase,
            "run_documentation_phase",
            lambda **_kwargs: DocumentationPhaseResult(summary="docs"),
        )
        monkeypatch.setattr(orch, "run_deliver", _run_deliver)

        task = Task(
            id="api",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            title="API",
            description="Build API",
            feature_branch_name="feature/review-api",
        )

        result = orch.BackendDevelopmentAgent(MagicMock()).run_workflow(
            repo_path=tmp_path,
            task=task,
            merge_to_development=False,
        )

        assert result.success is True
        assert git_agent.create_called is False
        assert captured["checkout"] == "feature/review-api"
        assert captured["existing_code"] == "existing branch code"
        assert events.index("checkout") < events.index("read_repo") < events.index("planning")
        assert captured["deliver_branch"] == "feature/review-api"

    def test_job_updater_failure_is_debug_logged_not_raised(self, tmp_path, monkeypatch):
        """A broken job_updater callback must not crash the workflow, and is logged at DEBUG."""
        from backend_code_v2_team import orchestrator as orch
        from backend_code_v2_team.models import (
            DeliverResult,
            DocumentationPhaseResult,
            ExecutionResult,
            PlanningResult,
        )

        from shared.dev_models.models import Task, TaskStatus, TaskType

        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n[tool.pytest.ini_options]\n")
        (tmp_path / "tests").mkdir()

        class _GitAgent:
            def create_feature_branch(self, *_args, **_kwargs):
                return True, "feature/api"

            def commit_current_changes(self, *_args, **_kwargs):
                return True, "committed"

        monkeypatch.setattr(orch, "checkout_branch", lambda *_a, **_kw: (True, "checked out"))
        monkeypatch.setattr(
            orch.BackendDevelopmentAgent,
            "_build_and_validate_tool_agents",
            lambda _self, _llm: {ToolAgentKind.GIT_BRANCH_MANAGEMENT: _GitAgent()},
        )
        monkeypatch.setattr(
            orch.BackendDevelopmentAgent, "_read_repo_code", lambda _self, _repo_path: ""
        )
        monkeypatch.setattr(
            orch,
            "run_planning",
            lambda **_kwargs: PlanningResult(microtasks=[Microtask(id="mt-1")], summary="planned"),
        )
        monkeypatch.setattr(
            orch,
            "run_execution_with_review_gates",
            lambda **_kwargs: ExecutionResult(
                files={"app.py": "print('ok')\n"},
                microtasks=[Microtask(id="mt-1", status=MicrotaskStatus.COMPLETED)],
                summary="implemented",
            ),
        )

        from backend_code_v2_team.phases import _profile as doc_phase

        monkeypatch.setattr(
            doc_phase,
            "run_documentation_phase",
            lambda **_kwargs: DocumentationPhaseResult(summary="docs"),
        )
        monkeypatch.setattr(
            orch,
            "run_deliver",
            lambda **kwargs: DeliverResult(
                branch_name=kwargs["feature_branch_name"], branch_ready=True, summary="ready"
            ),
        )

        mock_debug = MagicMock()
        monkeypatch.setattr(orch.logger, "debug", mock_debug)

        def bad_updater(**_kwargs):
            raise RuntimeError("job service down")

        task = Task(
            id="api",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            title="API",
            description="Build API",
        )

        result = orch.BackendDevelopmentAgent(MagicMock()).run_workflow(
            repo_path=tmp_path,
            task=task,
            merge_to_development=False,
            job_updater=bad_updater,
        )

        assert result.success is True
        assert mock_debug.called
        logged = " ".join(str(arg) for call in mock_debug.call_args_list for arg in call[0])
        assert "job_updater failed" in logged


class TestBackendDocAgentDeprecated:
    """doc_agent is accepted for backward compatibility but never forwarded downstream."""

    def _patch_success(self, monkeypatch, orch, doc_phase):
        from backend_code_v2_team.models import (
            DeliverResult,
            DocumentationPhaseResult,
            ExecutionResult,
            PlanningResult,
        )

        class _GitAgent:
            def create_feature_branch(self, *_args, **_kwargs):
                return True, "feature/api"

            def commit_current_changes(self, *_args, **_kwargs):
                return True, "committed"

        monkeypatch.setattr(orch, "checkout_branch", lambda *_a, **_kw: (True, "checked out"))
        monkeypatch.setattr(
            orch.BackendDevelopmentAgent,
            "_build_and_validate_tool_agents",
            lambda _self, _llm: {ToolAgentKind.GIT_BRANCH_MANAGEMENT: _GitAgent()},
        )
        monkeypatch.setattr(
            orch.BackendDevelopmentAgent, "_read_repo_code", lambda _self, _repo_path: ""
        )
        monkeypatch.setattr(
            orch,
            "run_planning",
            lambda **_kwargs: PlanningResult(microtasks=[Microtask(id="mt-1")], summary="planned"),
        )
        monkeypatch.setattr(
            orch,
            "run_execution_with_review_gates",
            lambda **_kwargs: ExecutionResult(
                files={"app.py": "print('ok')\n"},
                microtasks=[Microtask(id="mt-1", status=MicrotaskStatus.COMPLETED)],
                summary="implemented",
            ),
        )
        monkeypatch.setattr(
            doc_phase,
            "run_documentation_phase",
            lambda **_kwargs: DocumentationPhaseResult(summary="docs"),
        )
        monkeypatch.setattr(
            orch,
            "run_deliver",
            lambda **kwargs: DeliverResult(
                branch_name=kwargs["feature_branch_name"], branch_ready=True, summary="ready"
            ),
        )

    def _task(self):
        from shared.dev_models.models import Task, TaskStatus, TaskType

        return Task(
            id="api",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            title="API",
            description="Build API",
        )

    def test_dev_agent_warns_when_doc_agent_passed(self, tmp_path, monkeypatch):
        from backend_code_v2_team import orchestrator as orch
        from backend_code_v2_team.phases import _profile as doc_phase

        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n[tool.pytest.ini_options]\n")
        (tmp_path / "tests").mkdir()

        self._patch_success(monkeypatch, orch, doc_phase)

        with pytest.warns(DeprecationWarning, match="doc_agent"):
            result = orch.BackendDevelopmentAgent(MagicMock()).run_workflow(
                repo_path=tmp_path,
                task=self._task(),
                merge_to_development=False,
                doc_agent=MagicMock(),
            )

        assert result.success is True

    def test_dev_agent_no_warning_when_doc_agent_omitted(self, tmp_path, monkeypatch, recwarn):
        from backend_code_v2_team import orchestrator as orch
        from backend_code_v2_team.phases import _profile as doc_phase

        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n[tool.pytest.ini_options]\n")
        (tmp_path / "tests").mkdir()

        self._patch_success(monkeypatch, orch, doc_phase)

        result = orch.BackendDevelopmentAgent(MagicMock()).run_workflow(
            repo_path=tmp_path,
            task=self._task(),
            merge_to_development=False,
        )

        assert result.success is True
        assert not any(issubclass(w.category, DeprecationWarning) for w in recwarn.list)

    def test_team_lead_warns_even_when_setup_fails(self, tmp_path, monkeypatch):
        """The team-lead warns for doc_agent unconditionally, before the lint/test gate."""
        from backend_code_v2_team import orchestrator as orch

        monkeypatch.setattr(
            orch,
            "run_setup",
            lambda **_kwargs: SetupResult(linting_configured=False, testing_configured=False),
        )

        with pytest.warns(DeprecationWarning, match="doc_agent"):
            result = orch.BackendCodeV2TeamLead(MagicMock()).run_workflow(
                repo_path=tmp_path,
                task=self._task(),
                doc_agent=MagicMock(),
            )

        assert result.success is False
        assert "linting is not configured" in result.failure_reason.lower()

    def test_team_lead_warns_exactly_once_when_delegation_succeeds(self, tmp_path, monkeypatch):
        """The forwarded doc_agent=None to the dev agent must not double the warning."""
        from backend_code_v2_team import orchestrator as orch
        from backend_code_v2_team.models import BackendCodeV2WorkflowResult

        received: dict = {}

        class _DevelopmentAgent:
            def __init__(self, _llm):
                pass

            def run_workflow(self, **kwargs):
                received.update(kwargs)
                return BackendCodeV2WorkflowResult(task_id="api", success=True)

        monkeypatch.setattr(
            orch,
            "run_setup",
            lambda **_kwargs: SetupResult(linting_configured=True, testing_configured=True),
        )
        monkeypatch.setattr(orch, "BackendDevelopmentAgent", _DevelopmentAgent)

        with pytest.warns(DeprecationWarning, match="doc_agent") as record:
            result = orch.BackendCodeV2TeamLead(MagicMock()).run_workflow(
                repo_path=tmp_path,
                task=self._task(),
                merge_to_development=False,
                doc_agent=MagicMock(),
            )

        assert result.success is True
        assert received["doc_agent"] is None
        doc_agent_warnings = [w for w in record.list if issubclass(w.category, DeprecationWarning)]
        assert len(doc_agent_warnings) == 1


# ---------------------------------------------------------------------------
# Documentation self-review: team wiring around the shared helper
# (deep loop behavior is covered in test_shared_review_utils.py)
# ---------------------------------------------------------------------------

_DOC_REVIEW_RESPONSE = (
    "## QUALITY_SCORE ##\n0.95\n## END QUALITY_SCORE ##\n"
    "## IMPROVEMENTS ##\n- Clarified usage\n## END IMPROVEMENTS ##\n"
    "## FILE docs/readme.md ##\nRefined content\n"
    "## SUMMARY ##\nRefinements made\n## END SUMMARY ##"
)


class _RecordingDocClient(DummyLLMClient):
    """Records every user prompt and returns a canned doc-review response.

    ``DummyLLMClient.stream`` forwards the rendered user prompt to
    ``complete_json`` (one call per Agent invocation, no tools), so the recorded
    prompts are exactly what each documentation self-review pass showed the LLM.
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text
        self.prompts: list[str] = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.prompts.append(prompt)
        return self._text


class TestDocumentationSelfReviewWiring:
    """The team wrapper threads its own prompt, parser, and result type into the
    shared documentation self-review helper. The deep loop behavior (chunking,
    min-score, failure resilience, early-stop suppression) is covered once in
    ``test_shared_review_utils.py``.
    """

    def test_wraps_shared_helper_with_team_prompt_parser_and_result(self):
        from backend_code_v2_team.models import DocumentationSelfReviewResult
        from backend_code_v2_team.phases._profile import run_documentation_self_review
        from backend_code_v2_team.prompts import DOCUMENTATION_SELF_REVIEW_PROMPT

        client = _RecordingDocClient(_DOC_REVIEW_RESPONSE)
        result = run_documentation_self_review(
            llm=client,
            documentation={"docs/readme.md": "old"},
            code_files={"app/a.py": "A = 1"},
            task_description="task",
            min_iterations=1,
            max_iterations=1,
        )
        # The team's own result type is returned, and the team parser read the
        # team's template format (score 0.95 came out of _DOC_REVIEW_RESPONSE).
        assert isinstance(result, DocumentationSelfReviewResult)
        assert result.final_quality_score == 0.95
        assert "docs/readme.md" in result.documentation
        # The team's own prompt template was the one shown to the LLM.
        prefix = DOCUMENTATION_SELF_REVIEW_PROMPT.split("{", 1)[0]
        assert prefix and prefix in client.prompts[0]


class TestDbCSelfReviewWiring:
    """The backend GATE_CONFIG must point the DbC self-review seam at the shared,
    reusable ``run_dbc_comments_review`` callable so completed microtasks get DbC
    comment coverage by default (gated at the call site by ``enable_dbc_comments``).
    """

    def test_gate_config_wires_dbc_self_review(self):
        from backend_code_v2_team.phases._profile import GATE_CONFIG

        from software_engineering_team.shared.phases.dbc_phase import run_dbc_comments_review

        # Identity, not just truthiness: the shared loop and its tests rely on this
        # being the exact shared callable, not a team-local wrapper around it.
        assert GATE_CONFIG.run_dbc_self_review is run_dbc_comments_review

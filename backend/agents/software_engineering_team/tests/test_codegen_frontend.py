"""
Unit tests for the frontend-code-v2 team: models, phases, tool agents, orchestrator.
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
    """Returns a canned text response through the Strands ``stream()`` path."""

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


from software_engineering_team.codegen_team.models import (  # noqa: E402
    CodegenWorkflowResult,
    Microtask,
    MicrotaskStatus,
    Phase,
    PlanningResult,
    SetupResult,
    ToolAgentInput,
    ToolAgentKind,
    ToolAgentOutput,
)


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
        wr = CodegenWorkflowResult()
        assert not wr.success
        assert wr.current_phase == Phase.SETUP
        assert wr.iterations_used == 0
        assert wr.setup_result is None

    def test_phase_enum_includes_setup(self):
        assert Phase.SETUP.value == "setup"
        assert Phase.SETUP in Phase

    def test_tool_agent_kind_frontend_specific(self):
        assert ToolAgentKind.STATE_MANAGEMENT.value == "state_management"
        assert ToolAgentKind.UI_DESIGN.value == "ui_design"
        assert ToolAgentKind.GIT_BRANCH_MANAGEMENT in ToolAgentKind
        assert ToolAgentKind.BUILD_SPECIALIST in ToolAgentKind

    def test_setup_result_model(self):
        sr = SetupResult(repo_initialized=True, readme_created=True, branch_created=True)
        assert sr.repo_initialized

    def test_tool_agent_io(self):
        mt = Microtask(id="mt-test", description="test")
        inp = ToolAgentInput(microtask=mt, repo_path="/tmp/repo", language="angular")
        assert inp.language == "angular"
        out = ToolAgentOutput(files={"app.component.ts": "content"}, summary="done")
        assert out.success


class TestSetupPhase:
    def test_run_setup_on_existing_repo(self, tmp_path):
        """Verify setup on an existing repo stays on development without creating a branch."""
        from software_engineering_team.codegen_team.stacks.frontend.profile import run_setup

        init_repo_with_existing_development(tmp_path)
        result = run_setup(repo_path=tmp_path, task_title="My App")
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
        from software_engineering_team.codegen_team.stacks.frontend.profile import run_setup

        assert not (tmp_path / ".git").exists()
        result = run_setup(repo_path=tmp_path, task_title="New App")
        assert result.repo_initialized or (tmp_path / ".git").exists()
        assert result.summary

    def test_run_setup_commits_scaffolding_leaving_clean_tree(self, tmp_path):
        """Setup must commit its lint/test scaffolding so the tree stays clean.

        Uncommitted scaffolding on ``development`` is regenerated as untracked
        files on a later pass and blocks the development agent's checkout of the
        review feature branch.
        """
        from software_engineering_team.codegen_team.stacks.frontend.profile import run_setup

        init_repo_with_existing_development(tmp_path)
        run_setup(repo_path=tmp_path, task_title="My App")
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
        from software_engineering_team.codegen_team.stacks.frontend.profile import run_setup

        init_repo_with_existing_development(tmp_path)
        run_setup(repo_path=tmp_path, task_title="My App")
        subprocess.run(
            ["git", "checkout", "-b", "feature/task-1"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        (tmp_path / "feature_change.ts").write_text("export const x = 1;\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: pass 1"], cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "checkout", "development"], cwd=tmp_path, capture_output=True, check=True
        )
        run_setup(repo_path=tmp_path, task_title="My App")
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
        from software_engineering_team.codegen_team.stacks.frontend.profile import run_setup

        init_repo_with_existing_development(tmp_path)
        subprocess.run(
            ["git", "checkout", "development"], cwd=tmp_path, capture_output=True, check=True
        )
        (tmp_path / "unrelated.ts").write_text("export const y = 2;\n", encoding="utf-8")
        run_setup(repo_path=tmp_path, task_title="My App")
        status = subprocess.run(
            ["git", "status", "--porcelain", "unrelated.ts"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
            text=True,
        )
        assert status.stdout.strip() == "?? unrelated.ts"
        committed = subprocess.run(
            ["git", "ls-files", "unrelated.ts"],
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
        from software_engineering_team.codegen_team.stacks.frontend import profile as setup_mod

        init_repo_with_existing_development(tmp_path)
        monkeypatch.setattr(setup_mod, "commit_paths", lambda *a, **k: (False, "rejected by hook"))
        with caplog.at_level("WARNING"):
            setup_mod.run_setup(repo_path=tmp_path, task_title="My App")
        assert "not committed" in caplog.text.lower()

    def test_setup_commits_its_edit_to_already_dirty_config(self, tmp_path):
        """Setup's edit to a pre-existing dirty package.json must be committed.

        A dirty-delta approach would drop setup's added lint/test scripts when
        package.json was already dirty, leaving it dirty and re-blocking the
        later feature-branch checkout. The committed file must be clean after.
        """
        import json

        from software_engineering_team.codegen_team.stacks.frontend.profile import run_setup

        init_repo_with_existing_development(tmp_path)
        subprocess.run(
            ["git", "checkout", "development"], cwd=tmp_path, capture_output=True, check=True
        )
        # package.json present and dirty (no lint/test scripts) before setup runs.
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "demo", "scripts": {}}, indent=2), encoding="utf-8"
        )
        run_setup(repo_path=tmp_path, task_title="My App")
        status = subprocess.run(
            ["git", "status", "--porcelain", "package.json"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
            text=True,
        )
        assert status.stdout.strip() == ""  # setup's script edits committed, not left dirty
        scripts = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))["scripts"]
        assert "lint" in scripts and "test" in scripts

    def test_configure_quality_tooling_adds_config_to_handoff_branch(self, tmp_path):
        """A feature branch created before setup must get lint/test config on demand.

        Reproduces the coding-team handoff: the adapter creates the review branch
        from development *before* setup commits scaffolding there, so the branch
        lacks the eslint/vitest config until the dev-agent calls
        configure_quality_tooling on it.
        """
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            configure_quality_tooling,
            run_setup,
        )

        init_repo_with_existing_development(tmp_path)
        subprocess.run(
            ["git", "checkout", "development"], cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "checkout", "-b", "feature/task-1"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "development"], cwd=tmp_path, capture_output=True, check=True
        )
        run_setup(repo_path=tmp_path, task_title="My App")
        subprocess.run(
            ["git", "checkout", "feature/task-1"], cwd=tmp_path, capture_output=True, check=True
        )
        assert not list(tmp_path.glob("eslint.config.*"))  # branch has no eslint config yet

        lint_ok, test_ok = configure_quality_tooling(tmp_path)

        assert lint_ok and test_ok
        assert list(tmp_path.glob("eslint.config.*"))
        assert list(tmp_path.glob("vitest.config.*"))
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
            text=True,
        )
        assert status.stdout.strip() == ""  # config committed to the feature branch, tree clean


class TestSetupPhaseHooks:
    """Direct unit tests for the frontend lint/test detection hooks.

    These hooks moved from the (coverage-omitted) former ``phases/setup.py``
    into ``phases/_profile.py`` as part of unifying setup onto shared config;
    ``_profile.py`` is not coverage-omitted, so the Angular-project and
    unreadable/malformed-config branches need direct coverage here rather
    than relying solely on the happy-path exercised via ``run_setup`` above.
    """

    def test_ensure_linting_configured_creates_config_for_angular_project(self, tmp_path):
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            _ensure_linting_configured,
        )

        (tmp_path / "angular.json").write_text("{}", encoding="utf-8")
        written: set = set()
        assert _ensure_linting_configured(tmp_path, written) is True
        assert (tmp_path / "eslint.config.js").exists()
        assert "eslint.config.js" in written

    def test_ensure_linting_configured_angular_config_already_present(self, tmp_path):
        """An Angular project whose eslint.config.js already exists must not
        be overwritten (and must not be reported as newly written)."""
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            _ensure_linting_configured,
        )

        (tmp_path / "angular.json").write_text("{}", encoding="utf-8")
        (tmp_path / "eslint.config.js").write_text("// existing\n", encoding="utf-8")
        written: set = set()
        assert _ensure_linting_configured(tmp_path, written) is True
        assert written == set()
        assert (tmp_path / "eslint.config.js").read_text(encoding="utf-8") == "// existing\n"

    def test_ensure_testing_configured_malformed_package_json(self, tmp_path):
        """A malformed package.json must not raise while probing for an
        existing test script; setup falls through to creating vitest config."""
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            _ensure_testing_configured,
        )

        (tmp_path / "package.json").write_text("{not valid json", encoding="utf-8")
        written: set = set()
        assert _ensure_testing_configured(tmp_path, written) is True
        assert (tmp_path / "vitest.config.ts").exists()

    def test_ensure_testing_configured_creates_config_for_angular_project(self, tmp_path):
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            _ensure_testing_configured,
        )

        (tmp_path / "angular.json").write_text("{}", encoding="utf-8")
        written: set = set()
        assert _ensure_testing_configured(tmp_path, written) is True
        assert (tmp_path / "vitest.config.mts").exists()
        assert (tmp_path / "src" / "test-setup.ts").exists()
        assert "vitest.config.mts" in written
        assert "src/test-setup.ts" in written

    def test_ensure_package_script_handles_unreadable_package_json(self, tmp_path):
        """An unreadable package.json must not raise; the script is simply
        not added and no write is reported."""
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            _ensure_package_script,
        )

        # A directory named package.json makes read_text raise IsADirectoryError.
        (tmp_path / "package.json").mkdir()
        assert _ensure_package_script(tmp_path, "lint", "eslint .") is False


class TestPlanningPhase:
    def test_language_detection_angular(self, tmp_path):
        from shared.dev_models.models import Task, TaskStatus, TaskType
        from software_engineering_team.codegen_team.stacks.frontend.profile import _detect_language

        (tmp_path / "angular.json").write_text("{}")
        task = Task(
            id="t1",
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
            description="build ui",
        )
        assert _detect_language(tmp_path, task) == "angular"

    def test_language_detection_from_description(self, tmp_path):
        from shared.dev_models.models import Task, TaskStatus, TaskType
        from software_engineering_team.codegen_team.stacks.frontend.profile import _detect_language

        task = Task(
            id="t1",
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
            description="Use React and TypeScript",
        )
        assert _detect_language(tmp_path, task) == "react"

    def test_parse_planning_output(self):
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            _parse_planning_output,
        )

        raw = {
            "microtasks": [
                {
                    "id": "mt-1",
                    "title": "Add component",
                    "tool_agent": "ui_design",
                    "description": "create component",
                },
                {
                    "id": "mt-2",
                    "title": "Add tests",
                    "tool_agent": "testing_qa",
                    "description": "unit tests",
                    "depends_on": ["mt-1"],
                },
            ],
            "language": "angular",
            "summary": "Plan created",
        }
        result = _parse_planning_output(raw, "typescript")
        assert len(result.microtasks) == 2
        assert result.microtasks[0].tool_agent == ToolAgentKind.UI_DESIGN
        assert result.microtasks[1].depends_on == ["mt-1"]
        assert result.language == "angular"

    def test_run_planning_fallback(self, tmp_path):
        from shared.dev_models.models import Task, TaskStatus, TaskType
        from software_engineering_team.codegen_team.stacks.frontend.profile import run_planning

        mock_llm = _TextStubClient(
            "## MICROTASKS ##\n## END MICROTASKS ##\n"
            "## LANGUAGE ##\ntypescript\n## END LANGUAGE ##\n"
            "## SUMMARY ##\nempty\n## END SUMMARY ##"
        )
        task = Task(
            id="t1",
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
            description="build something",
        )
        result = run_planning(llm=mock_llm, task=task, repo_path=tmp_path)
        assert len(result.microtasks) == 1
        assert result.microtasks[0].id == "mt-implement-task"


class TestToolAgents:
    def test_build_tool_agents_includes_all_kinds(self):
        from software_engineering_team.codegen_team.orchestrator import _build_frontend_tool_agents

        agents = _build_frontend_tool_agents(MagicMock())
        assert ToolAgentKind.GIT_BRANCH_MANAGEMENT in agents
        assert ToolAgentKind.BUILD_SPECIALIST in agents
        assert ToolAgentKind.UI_DESIGN in agents
        assert hasattr(agents[ToolAgentKind.GIT_BRANCH_MANAGEMENT], "create_feature_branch")
        assert hasattr(agents[ToolAgentKind.GIT_BRANCH_MANAGEMENT], "commit_current_changes")
        assert hasattr(agents[ToolAgentKind.GIT_BRANCH_MANAGEMENT], "deliver")

    def test_git_agent_create_feature_branch(self, tmp_path):
        import subprocess

        from software_engineering_team.shared.tool_agent_git_branch import (
            GitBranchManagementToolAgent,
        )

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        (tmp_path / "f").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "branch", "-m", "development"], cwd=tmp_path, capture_output=True, check=True
        )
        agent = GitBranchManagementToolAgent()
        ok, name = agent.create_feature_branch(tmp_path, "task-1", "Login page")
        assert ok is True
        assert name

    def test_git_agent_commit_current_changes(self, tmp_path):
        from software_engineering_team.shared.tool_agent_git_branch import (
            GitBranchManagementToolAgent,
        )

        (tmp_path / ".git").mkdir()
        agent = GitBranchManagementToolAgent()
        ok, msg = agent.commit_current_changes(tmp_path, "wip: test")
        assert isinstance(ok, bool)
        assert isinstance(msg, str)

    def test_build_specialist_stub(self):
        from software_engineering_team.codegen_team.tool_agents.frontend.build_specialist import (
            BuildSpecialistAdapterAgent,
        )

        agent = BuildSpecialistAdapterAgent()
        out = agent.execute(ToolAgentInput(microtask=Microtask(id="mt-1"), repo_path="/tmp"))
        assert out.summary
        assert hasattr(agent, "plan")
        assert hasattr(agent, "review")
        assert hasattr(agent, "problem_solve")
        assert hasattr(agent, "deliver")


class TestCodegenDevelopmentAgentBranchReuse:
    def test_existing_feature_branch_is_reused_without_recreation(self, tmp_path, monkeypatch):
        """Revision workflows keep the reviewed branch instead of recreating it from development."""
        from shared.dev_models.models import Task, TaskStatus, TaskType
        from software_engineering_team.codegen_team import orchestrator as orch
        from software_engineering_team.codegen_team.models import (
            DeliverResult,
            DocumentationPhaseResult,
            ExecutionResult,
            PlanningResult,
        )

        (tmp_path / "eslint.config.js").write_text("export default [];\n")
        (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}\n')

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
                files={"src/app.component.ts": "export class AppComponent {}\n"},
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

        from software_engineering_team.codegen_team.stacks.frontend import profile as doc_phase

        monkeypatch.setattr(orch, "checkout_branch", _checkout_branch)
        monkeypatch.setattr(
            orch.CodegenDevelopmentAgent,
            "_build_and_validate_tool_agents",
            lambda _self, _llm: {ToolAgentKind.GIT_BRANCH_MANAGEMENT: git_agent},
        )
        monkeypatch.setattr(orch.CodegenDevelopmentAgent, "_read_repo_code", _read_repo_code)
        monkeypatch.setattr(orch, "_frontend_run_planning", _run_planning)
        monkeypatch.setattr(
            orch, "_frontend_run_execution_with_review_gates", _run_execution_with_review_gates
        )
        monkeypatch.setattr(
            doc_phase,
            "run_documentation_phase",
            lambda **_kwargs: DocumentationPhaseResult(summary="docs"),
        )
        monkeypatch.setattr(orch, "_frontend_run_deliver", _run_deliver)

        task = Task(
            id="ui",
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
            title="UI",
            description="Build UI",
            feature_branch_name="feature/review-ui",
        )

        result = orch.CodegenDevelopmentAgent(MagicMock(), "frontend").run_workflow(
            repo_path=tmp_path,
            task=task,
            merge_to_development=False,
        )

        assert result.success is True
        assert git_agent.create_called is False
        assert captured["checkout"] == "feature/review-ui"
        assert captured["existing_code"] == "existing branch code"
        assert events.index("checkout") < events.index("read_repo") < events.index("planning")
        assert captured["deliver_branch"] == "feature/review-ui"

    def test_job_updater_failure_is_debug_logged_not_raised(self, tmp_path, monkeypatch):
        """A broken job_updater callback must not crash the workflow, and is logged at DEBUG."""
        from shared.dev_models.models import Task, TaskStatus, TaskType
        from software_engineering_team.codegen_team import orchestrator as orch
        from software_engineering_team.codegen_team.models import (
            DeliverResult,
            DocumentationPhaseResult,
            ExecutionResult,
            PlanningResult,
        )

        (tmp_path / "eslint.config.js").write_text("export default [];\n")
        (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}\n')

        class _GitAgent:
            def create_feature_branch(self, *_args, **_kwargs):
                return True, "feature/ui"

            def commit_current_changes(self, *_args, **_kwargs):
                return True, "committed"

        monkeypatch.setattr(orch, "checkout_branch", lambda *_a, **_kw: (True, "checked out"))
        monkeypatch.setattr(
            orch.CodegenDevelopmentAgent,
            "_build_and_validate_tool_agents",
            lambda _self, _llm: {ToolAgentKind.GIT_BRANCH_MANAGEMENT: _GitAgent()},
        )
        monkeypatch.setattr(
            orch.CodegenDevelopmentAgent, "_read_repo_code", lambda _self, _repo_path: ""
        )
        monkeypatch.setattr(
            orch,
            "_frontend_run_planning",
            lambda **_kwargs: PlanningResult(microtasks=[Microtask(id="mt-1")], summary="planned"),
        )
        monkeypatch.setattr(
            orch,
            "_frontend_run_execution_with_review_gates",
            lambda **_kwargs: ExecutionResult(
                files={"src/app.component.ts": "export class AppComponent {}\n"},
                microtasks=[Microtask(id="mt-1", status=MicrotaskStatus.COMPLETED)],
                summary="implemented",
            ),
        )

        from software_engineering_team.codegen_team.stacks.frontend import profile as doc_phase

        monkeypatch.setattr(
            doc_phase,
            "run_documentation_phase",
            lambda **_kwargs: DocumentationPhaseResult(summary="docs"),
        )
        monkeypatch.setattr(
            orch,
            "_frontend_run_deliver",
            lambda **kwargs: DeliverResult(
                branch_name=kwargs["feature_branch_name"], branch_ready=True, summary="ready"
            ),
        )

        mock_debug = MagicMock()
        monkeypatch.setattr(orch.logger, "debug", mock_debug)

        def bad_updater(**_kwargs):
            raise RuntimeError("job service down")

        task = Task(
            id="ui",
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
            title="UI",
            description="Build UI",
        )

        result = orch.CodegenDevelopmentAgent(MagicMock(), "frontend").run_workflow(
            repo_path=tmp_path,
            task=task,
            merge_to_development=False,
            job_updater=bad_updater,
        )

        assert result.success is True
        assert mock_debug.called
        logged = " ".join(str(arg) for call in mock_debug.call_args_list for arg in call[0])
        assert "job_updater failed" in logged


class TestFrontendDocAgentDeprecated:
    """doc_agent is accepted for backward compatibility but never forwarded downstream."""

    def _patch_success(self, monkeypatch, orch, doc_phase):
        from software_engineering_team.codegen_team.models import (
            DeliverResult,
            DocumentationPhaseResult,
            ExecutionResult,
            PlanningResult,
        )

        class _GitAgent:
            def create_feature_branch(self, *_args, **_kwargs):
                return True, "feature/ui"

            def commit_current_changes(self, *_args, **_kwargs):
                return True, "committed"

        monkeypatch.setattr(orch, "checkout_branch", lambda *_a, **_kw: (True, "checked out"))
        monkeypatch.setattr(
            orch.CodegenDevelopmentAgent,
            "_build_and_validate_tool_agents",
            lambda _self, _llm: {ToolAgentKind.GIT_BRANCH_MANAGEMENT: _GitAgent()},
        )
        monkeypatch.setattr(
            orch.CodegenDevelopmentAgent, "_read_repo_code", lambda _self, _repo_path: ""
        )
        monkeypatch.setattr(
            orch,
            "_frontend_run_planning",
            lambda **_kwargs: PlanningResult(microtasks=[Microtask(id="mt-1")], summary="planned"),
        )
        monkeypatch.setattr(
            orch,
            "_frontend_run_execution_with_review_gates",
            lambda **_kwargs: ExecutionResult(
                files={"src/app.component.ts": "export class AppComponent {}\n"},
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
            "_frontend_run_deliver",
            lambda **kwargs: DeliverResult(
                branch_name=kwargs["feature_branch_name"], branch_ready=True, summary="ready"
            ),
        )

    def _task(self):
        from shared.dev_models.models import Task, TaskStatus, TaskType

        return Task(
            id="ui",
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
            title="UI",
            description="Build UI",
        )

    def test_dev_agent_warns_when_doc_agent_passed(self, tmp_path, monkeypatch):
        from software_engineering_team.codegen_team import orchestrator as orch
        from software_engineering_team.codegen_team.stacks.frontend import profile as doc_phase

        (tmp_path / "eslint.config.js").write_text("export default [];\n")
        (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}\n')

        self._patch_success(monkeypatch, orch, doc_phase)

        with pytest.warns(DeprecationWarning, match="doc_agent"):
            result = orch.CodegenDevelopmentAgent(MagicMock(), "frontend").run_workflow(
                repo_path=tmp_path,
                task=self._task(),
                merge_to_development=False,
                doc_agent=MagicMock(),
            )

        assert result.success is True

    def test_dev_agent_no_warning_when_doc_agent_omitted(self, tmp_path, monkeypatch, recwarn):
        from software_engineering_team.codegen_team import orchestrator as orch
        from software_engineering_team.codegen_team.stacks.frontend import profile as doc_phase

        (tmp_path / "eslint.config.js").write_text("export default [];\n")
        (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}\n')

        self._patch_success(monkeypatch, orch, doc_phase)

        result = orch.CodegenDevelopmentAgent(MagicMock(), "frontend").run_workflow(
            repo_path=tmp_path,
            task=self._task(),
            merge_to_development=False,
        )

        assert result.success is True
        assert not any(issubclass(w.category, DeprecationWarning) for w in recwarn.list)

    def test_team_lead_warns_even_when_setup_fails(self, tmp_path, monkeypatch):
        """The team-lead warns for doc_agent unconditionally, before the lint/test gate."""
        from software_engineering_team.codegen_team import orchestrator as orch

        monkeypatch.setattr(
            orch,
            "_frontend_run_setup",
            lambda **_kwargs: SetupResult(linting_configured=False, testing_configured=False),
        )

        with pytest.warns(DeprecationWarning, match="doc_agent"):
            result = orch.CodegenTeamLead(MagicMock(), "frontend").run_workflow(
                repo_path=tmp_path,
                task=self._task(),
                doc_agent=MagicMock(),
            )

        assert result.success is False
        assert "linting is not configured" in result.failure_reason.lower()

    def test_team_lead_warns_exactly_once_when_delegation_succeeds(self, tmp_path, monkeypatch):
        """The forwarded doc_agent=None to the dev agent must not double the warning."""
        from software_engineering_team.codegen_team import orchestrator as orch
        from software_engineering_team.codegen_team.models import CodegenWorkflowResult

        received: dict = {}

        class _DevelopmentAgent:
            def __init__(self, _llm, _stack="frontend"):
                pass

            def run_workflow(self, **kwargs):
                received.update(kwargs)
                return CodegenWorkflowResult(task_id="ui", success=True)

        monkeypatch.setattr(
            orch,
            "_frontend_run_setup",
            lambda **_kwargs: SetupResult(linting_configured=True, testing_configured=True),
        )
        monkeypatch.setattr(orch, "CodegenDevelopmentAgent", _DevelopmentAgent)

        with pytest.warns(DeprecationWarning, match="doc_agent") as record:
            result = orch.CodegenTeamLead(MagicMock(), "frontend").run_workflow(
                repo_path=tmp_path,
                task=self._task(),
                merge_to_development=False,
                doc_agent=MagicMock(),
            )

        assert result.success is True
        assert received["doc_agent"] is None
        doc_agent_warnings = [w for w in record.list if issubclass(w.category, DeprecationWarning)]
        assert len(doc_agent_warnings) == 1


class TestCodegenDevelopmentAgent:
    def test_build_tool_runners(self):
        from software_engineering_team.codegen_team.models import ToolAgentKind
        from software_engineering_team.codegen_team.orchestrator import CodegenDevelopmentAgent
        from software_engineering_team.codegen_team.tool_agents.frontend.state_management import (
            StateManagementToolAgent,
        )
        from software_engineering_team.shared.tool_agent_git_branch import (
            GitBranchManagementToolAgent,
        )

        agent = CodegenDevelopmentAgent(MagicMock(), "frontend")
        tool_agents = {
            ToolAgentKind.STATE_MANAGEMENT: StateManagementToolAgent(),
            ToolAgentKind.GIT_BRANCH_MANAGEMENT: GitBranchManagementToolAgent(),
        }
        runners = agent._build_tool_runners(tool_agents)
        assert ToolAgentKind.STATE_MANAGEMENT in runners
        assert ToolAgentKind.GIT_BRANCH_MANAGEMENT in runners


class TestCodegenTeamLead:
    def test_team_lead_propagates_development_handoff_fields(self, tmp_path, monkeypatch):
        """Team-lead result preserves the inner development handoff fields."""
        from shared.dev_models.models import Task, TaskStatus, TaskType
        from software_engineering_team.codegen_team import orchestrator as orch
        from software_engineering_team.codegen_team.models import (
            CodegenWorkflowResult,
            DeliverResult,
            DocumentationPhaseResult,
            Phase,
            SetupResult,
        )

        deliver = DeliverResult(
            branch_name="feature/ui",
            branch_ready=True,
            delivered_files=["src/app.component.ts"],
            summary="handoff ready",
        )
        documentation = DocumentationPhaseResult(summary="docs updated")
        inner = CodegenWorkflowResult(
            task_id="ui",
            success=True,
            current_phase=Phase.DELIVER,
            iterations_used=2,
            documentation_result=documentation,
            deliver_result=deliver,
            final_files={"src/app.component.ts": "export class AppComponent {}\n"},
            summary="implemented and ready",
            failure_reason="",
            needs_followup=True,
        )

        class _DevelopmentAgent:
            def __init__(self, _llm, _stack="frontend"):
                pass

            def run_workflow(self, **_kwargs):
                return inner

        monkeypatch.setattr(
            orch,
            "_frontend_run_setup",
            lambda **_kwargs: SetupResult(linting_configured=True, testing_configured=True),
        )
        monkeypatch.setattr(orch, "CodegenDevelopmentAgent", _DevelopmentAgent)

        task = Task(
            id="ui",
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
            title="UI",
            description="Build UI",
        )

        result = orch.CodegenTeamLead(MagicMock(), "frontend").run_workflow(
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
        assert result.final_files == {"src/app.component.ts": "export class AppComponent {}\n"}
        assert result.summary == "implemented and ready"
        assert result.needs_followup is True


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
        from software_engineering_team.codegen_team.models import DocumentationSelfReviewResult
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            run_documentation_self_review,
        )
        from software_engineering_team.codegen_team.stacks.frontend.prompts import (
            DOCUMENTATION_SELF_REVIEW_PROMPT,
        )

        client = _RecordingDocClient(_DOC_REVIEW_RESPONSE)
        result = run_documentation_self_review(
            llm=client,
            documentation={"docs/readme.md": "old"},
            code_files={"src/a.ts": "export const a = 1;"},
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
    """The frontend GATE_CONFIG must point the DbC self-review seam at the shared,
    reusable ``run_dbc_comments_review`` callable so completed frontend microtasks
    get DbC comment coverage by default (gated at the call site by
    ``enable_dbc_comments``). Frontend's non-Python files have no AST-level
    insertion safety net of their own, so the shared phase's post-insertion
    build-verification revert is exercised here as the sole guard against a bad
    DbC edit reaching a commit.
    """

    def test_gate_config_wires_dbc_self_review(self):
        from software_engineering_team.codegen_team.stacks.frontend.profile import GATE_CONFIG
        from software_engineering_team.shared.phases.dbc_phase import run_dbc_comments_review

        # Identity, not just truthiness: the shared loop and its tests rely on this
        # being the exact shared callable, not a team-local wrapper around it.
        assert GATE_CONFIG.run_dbc_self_review is run_dbc_comments_review

    @staticmethod
    def _drive_dbc(*, tmp_path, monkeypatch, dbc_files, build_verifier, microtask_files):
        """Run the shared DbC self-review through the *frontend* GATE_CONFIG.

        Monkeypatches ``DbcCommentsAgent`` so the wired ``run_dbc_comments_review``
        returns ``dbc_files`` for the concatenated frontend input, then invokes the
        phase orchestrator with the frontend team's ``GATE_CONFIG`` and the given
        ``build_verifier``. Returns ``(mt, microtask_files, all_files)`` after the
        in-place mutations.
        """
        from types import SimpleNamespace

        from software_engineering_team.codegen_team.stacks.frontend.profile import GATE_CONFIG
        from software_engineering_team.shared.phases import dbc_phase
        from software_engineering_team.shared.phases.dbc_phase import _run_dbc_self_review
        from software_engineering_team.shared.phases.execution import ReviewDependencies
        from software_engineering_team.technical_writers.dbc_comments_agent.models import (
            DbcCommentsOutput,
        )

        class _FakeDbcAgent:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def run(self, input_data: Any, on_status: Optional[Any] = None) -> DbcCommentsOutput:
                return DbcCommentsOutput(files=dict(dbc_files))

        monkeypatch.setattr(dbc_phase, "DbcCommentsAgent", _FakeDbcAgent)

        mt = SimpleNamespace(id="mt-1", title="Microtask", output_files=dict(microtask_files))
        all_files = dict(microtask_files)
        _run_dbc_self_review(
            gate_config=GATE_CONFIG,
            task=SimpleNamespace(id="t1", title="T", description="build a component"),
            task_id="t1",
            mt=mt,
            microtask_files=microtask_files,
            repo_path=tmp_path,
            all_files=all_files,
            architecture=None,
            language="typescript",
            deps=ReviewDependencies(build_verifier=build_verifier),
            build_verify_label="build",
            progress_callback=None,
            current_idx=0,
            completed_ids=set(),
            total=1,
            detail_cb=lambda d, idx, phase: None,
        )
        return mt, microtask_files, all_files

    def test_frontend_microtask_gets_dbc_comments_when_build_passes(self, tmp_path, monkeypatch):
        # A completed frontend microtask's .tsx file gains DbC-inserted comments,
        # persisted to disk and reflected in every in-memory map, once the
        # post-insertion build verification passes.
        (tmp_path / "src").mkdir()
        original = "export const Button = () => null;\n"
        (tmp_path / "src" / "button.tsx").write_text(original, encoding="utf-8")
        with_dbc = "// Preconditions: none.\n" + original
        microtask_files = {"src/button.tsx": original}

        mt, microtask_files, all_files = self._drive_dbc(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            dbc_files={"src/button.tsx": with_dbc},
            build_verifier=lambda repo, label, tid: (True, "ok"),
            microtask_files=microtask_files,
        )

        assert (tmp_path / "src" / "button.tsx").read_text(encoding="utf-8") == with_dbc
        assert microtask_files == {"src/button.tsx": with_dbc}
        assert all_files == microtask_files
        assert mt.output_files == microtask_files

    def test_frontend_build_failure_after_dbc_reverts_only_affected_file(
        self, tmp_path, monkeypatch
    ):
        # The build-safety net: when build verification fails after DbC insertion,
        # only the DbC-touched file is reverted (on disk and in every map); a
        # sibling frontend file the DbC step never touched is left untouched. This
        # is the guard frontend relies on in place of an AST-level safety net.
        (tmp_path / "src").mkdir()
        original_button = "export const Button = () => null;\n"
        original_input = "export const Input = () => null;\n"
        (tmp_path / "src" / "button.tsx").write_text(original_button, encoding="utf-8")
        (tmp_path / "src" / "input.tsx").write_text(original_input, encoding="utf-8")
        microtask_files = {"src/button.tsx": original_button, "src/input.tsx": original_input}

        mt, microtask_files, all_files = self._drive_dbc(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            # DbC only proposes an edit to button.tsx; input.tsx is never touched.
            dbc_files={"src/button.tsx": "// Preconditions: none.\n" + original_button},
            build_verifier=lambda repo, label, tid: (False, "tsc failed"),
            microtask_files=microtask_files,
        )

        # Affected file reverted to its pre-DbC content everywhere; sibling intact.
        assert (tmp_path / "src" / "button.tsx").read_text(encoding="utf-8") == original_button
        assert (tmp_path / "src" / "input.tsx").read_text(encoding="utf-8") == original_input
        expected = {"src/button.tsx": original_button, "src/input.tsx": original_input}
        assert microtask_files == expected
        assert all_files == expected
        assert mt.output_files == expected

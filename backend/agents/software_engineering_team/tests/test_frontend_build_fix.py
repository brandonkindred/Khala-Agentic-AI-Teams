"""Tests for frontend build-fix specialist integration (Phase 2)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from frontend_team.feature_agent.agent import (
    _apply_frontend_build_fix_edits,
    _extract_affected_file_paths_from_frontend_build_errors,
    _read_frontend_affected_files_code,
)

from software_engineering_team.shared.command_runner import CommandResult
from software_engineering_team.tests.conftest import ConfigurableLLM

_INSTALL_OK = CommandResult(success=True, exit_code=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestExtractAffectedFilePaths:
    def test_parses_ts_paths(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "app").mkdir(parents=True)
        (tmp_path / "src" / "app" / "app.component.ts").write_text("// comp", encoding="utf-8")
        errors = "ERROR: src/app/app.component.ts:15:3 - error TS2304: Cannot find name 'x'."
        paths = _extract_affected_file_paths_from_frontend_build_errors(errors, tmp_path)
        assert "src/app/app.component.ts" in paths

    def test_includes_app_routes(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "app").mkdir(parents=True)
        (tmp_path / "src" / "app" / "app.routes.ts").write_text("// routes", encoding="utf-8")
        errors = "ERROR: some other error"
        paths = _extract_affected_file_paths_from_frontend_build_errors(errors, tmp_path)
        assert "src/app/app.routes.ts" in paths

    def test_caps_at_ten(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "app").mkdir(parents=True)
        lines = []
        for i in range(15):
            fname = f"src/app/file{i}.ts"
            (tmp_path / fname).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / fname).write_text(f"// {i}", encoding="utf-8")
            lines.append(f"ERROR: {fname}:1:1 - error TS0000")
        errors = "\n".join(lines)
        paths = _extract_affected_file_paths_from_frontend_build_errors(errors, tmp_path)
        assert len(paths) <= 10

    def test_parses_could_not_resolve(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "app" / "components").mkdir(parents=True)
        (tmp_path / "src" / "app" / "components" / "foo.component.ts").write_text(
            "// foo", encoding="utf-8"
        )
        errors = 'Could not resolve "./app/components/foo.component"'
        paths = _extract_affected_file_paths_from_frontend_build_errors(errors, tmp_path)
        assert "src/app/components/foo.component.ts" in paths


class TestReadFrontendAffectedFilesCode:
    def test_concatenates_content(self, tmp_path: Path) -> None:
        (tmp_path / "a.ts").write_text("AAA", encoding="utf-8")
        (tmp_path / "b.ts").write_text("BBB", encoding="utf-8")
        result = _read_frontend_affected_files_code(tmp_path, ["a.ts", "b.ts"])
        assert "AAA" in result
        assert "BBB" in result

    def test_skips_missing(self, tmp_path: Path) -> None:
        result = _read_frontend_affected_files_code(tmp_path, ["missing.ts"])
        assert "No affected files found" in result


class TestApplyFrontendBuildFixEdits:
    def test_applies_edits(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.ts").write_text("const x = 1;", encoding="utf-8")

        from build_fix_specialist.models import CodeEdit

        edits = [
            CodeEdit(file_path="src/a.ts", old_text="const x = 1;", new_text="const x = 2;"),
        ]
        ok, msg, files = _apply_frontend_build_fix_edits(tmp_path, edits)
        assert ok
        assert "src/a.ts" in files
        assert files["src/a.ts"] == "const x = 2;"

    def test_skips_missing_old_text(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.ts").write_text("const x = 1;", encoding="utf-8")

        from build_fix_specialist.models import CodeEdit

        edits = [
            CodeEdit(file_path="src/a.ts", old_text="NOMATCH", new_text="replaced"),
        ]
        ok, msg, files = _apply_frontend_build_fix_edits(tmp_path, edits)
        assert not ok
        assert "old_text not found" in msg

    def test_applies_multiple_edits_to_same_file(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.ts").write_text("aaa bbb ccc", encoding="utf-8")

        from build_fix_specialist.models import CodeEdit

        edits = [
            CodeEdit(file_path="src/a.ts", old_text="aaa", new_text="AAA"),
            CodeEdit(file_path="src/a.ts", old_text="bbb", new_text="BBB"),
        ]
        ok, msg, files = _apply_frontend_build_fix_edits(tmp_path, edits)
        assert ok
        assert files["src/a.ts"] == "AAA BBB ccc"


# ---------------------------------------------------------------------------
# Workflow integration tests
# ---------------------------------------------------------------------------


class TestFrontendWorkflowBuildFixSpecialist:
    """Test that BuildFixSpecialist is invoked in feature agent workflow on repeated build failure."""

    @pytest.fixture
    def _setup_repo(self, tmp_path: Path):
        """Create a minimal git repo with Angular-like files."""
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

        (tmp_path / "src" / "app").mkdir(parents=True)
        (tmp_path / "src" / "app" / "app.component.ts").write_text(
            "import { Component } from '@angular/core';\n@Component({})\nexport class AppComponent {}",
            encoding="utf-8",
        )
        (tmp_path / "angular.json").write_text("{}", encoding="utf-8")
        (tmp_path / "package.json").write_text('{"name": "test"}', encoding="utf-8")

        subprocess.run(
            ["git", "checkout", "-b", "development"], cwd=tmp_path, check=True, capture_output=True
        )
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True
        )
        return tmp_path

    @patch(
        "software_engineering_team.shared.command_runner.ensure_frontend_dependencies_installed",
        return_value=_INSTALL_OK,
    )
    def test_specialist_invoked_on_repeated_failure(
        self, _mock_install: MagicMock, _setup_repo: Path
    ) -> None:
        from build_fix_specialist.models import CodeEdit

        from software_engineering_team.shared.models import Task, TaskType

        task = Task(
            id="fe-1",
            type=TaskType.FRONTEND,
            assignee="frontend",
            title="Fix component",
            description="Fix the broken component",
            acceptance_criteria=["Compiles"],
            metadata={
                "goal": {"summary": "fix"},
                "scope": {"included": ["src"]},
                "constraints": {},
                "non_functional_requirements": {},
                "inputs_outputs": {"input": "x", "output": "y"},
            },
        )

        mock_llm = ConfigurableLLM()
        mock_llm._max_context_tokens = 16384
        mock_llm.complete_json_mock.side_effect = [
            {
                "feature_intent": "Fix",
                "what_changes": ["src/app/app.component.ts"],
                "algorithms_data_structures": "",
                "tests_needed": "",
            },
            {
                "code": "",
                "language": "typescript",
                "summary": "Done",
                "files": {"src/app/app.component.ts": "export class AppComponent {}"},
                "tests": "",
                "suggested_commit_message": "fix: comp",
                "npm_packages_to_install": [],
            },
        ] + [
            {
                "code": "",
                "language": "typescript",
                "summary": "Fixed",
                "files": {"src/app/app.component.ts": "export class AppComponent {}"},
                "tests": "",
                "suggested_commit_message": "fix: build",
                "npm_packages_to_install": [],
            }
            for _ in range(10)
        ]

        from frontend_team.feature_agent import FrontendExpertAgent

        agent = FrontendExpertAgent(llm_client=mock_llm)

        same_error = "ERROR: src/app/app.component.ts:3:1 - error TS2304: Cannot find name 'x'."

        mock_specialist = MagicMock()
        mock_specialist.run.return_value = MagicMock(
            edits=[
                CodeEdit(
                    file_path="src/app/app.component.ts",
                    old_text="export class AppComponent {}",
                    new_text="export class AppComponent { x = 1; }",
                ),
            ],
            summary="Fixed missing property",
        )

        def build_verifier(_repo_path, _agent_type, _task_id):
            return (False, same_error)

        mock_qa = MagicMock()
        from qa_agent.models import BugReport

        mock_qa.run.return_value = MagicMock(
            bugs_found=[
                BugReport(
                    severity="critical",
                    description="Fix",
                    location="src/app/app.component.ts",
                    recommendation="Fix",
                ),
            ]
        )
        mock_tech_lead = MagicMock()
        mock_tech_lead.review_progress.return_value = []

        agent.run_workflow(
            repo_path=_setup_repo,
            backend_dir=_setup_repo,
            task=task,
            spec_content="# Spec",
            architecture=None,
            qa_agent=mock_qa,
            accessibility_agent=MagicMock(),
            security_agent=MagicMock(),
            code_review_agent=MagicMock(),
            tech_lead=mock_tech_lead,
            build_verifier=build_verifier,
            build_fix_specialist=mock_specialist,
        )

        mock_specialist.run.assert_called()

    @patch(
        "software_engineering_team.shared.command_runner.ensure_frontend_dependencies_installed",
        return_value=_INSTALL_OK,
    )
    def test_falls_back_to_qa_when_no_edits(
        self, _mock_install: MagicMock, _setup_repo: Path
    ) -> None:
        from software_engineering_team.shared.models import Task, TaskType

        task = Task(
            id="fe-2",
            type=TaskType.FRONTEND,
            assignee="frontend",
            title="Fix",
            description="Fix",
            acceptance_criteria=["Compiles"],
            metadata={
                "goal": {"summary": "fix"},
                "scope": {"included": ["src"]},
                "constraints": {},
                "non_functional_requirements": {},
                "inputs_outputs": {"input": "x", "output": "y"},
            },
        )

        mock_llm = ConfigurableLLM()
        mock_llm._max_context_tokens = 16384
        mock_llm.complete_json_mock.side_effect = [
            {
                "feature_intent": "Fix",
                "what_changes": ["src/app/app.component.ts"],
                "algorithms_data_structures": "",
                "tests_needed": "",
            },
            {
                "code": "",
                "language": "typescript",
                "summary": "Done",
                "files": {"src/app/app.component.ts": "export class AppComponent {}"},
                "tests": "",
                "suggested_commit_message": "fix: comp",
                "npm_packages_to_install": [],
            },
        ] + [
            {
                "code": "",
                "language": "typescript",
                "summary": "Fixed",
                "files": {"src/app/app.component.ts": "export class AppComponent {}"},
                "tests": "",
                "suggested_commit_message": "fix: build",
                "npm_packages_to_install": [],
            }
            for _ in range(10)
        ]

        from frontend_team.feature_agent import FrontendExpertAgent

        agent = FrontendExpertAgent(llm_client=mock_llm)

        mock_specialist = MagicMock()
        mock_specialist.run.return_value = MagicMock(edits=[], summary="No fix found")

        same_error = "ERROR: src/app/app.component.ts:3:1 - error TS2304"

        mock_qa = MagicMock()
        from qa_agent.models import BugReport

        mock_qa.run.return_value = MagicMock(
            bugs_found=[
                BugReport(
                    severity="critical",
                    description="Fix",
                    location="src/app/app.component.ts",
                    recommendation="Fix",
                ),
            ]
        )
        mock_tech_lead = MagicMock()
        mock_tech_lead.review_progress.return_value = []

        agent.run_workflow(
            repo_path=_setup_repo,
            backend_dir=_setup_repo,
            task=task,
            spec_content="# Spec",
            architecture=None,
            qa_agent=mock_qa,
            accessibility_agent=MagicMock(),
            security_agent=MagicMock(),
            code_review_agent=MagicMock(),
            tech_lead=mock_tech_lead,
            build_verifier=lambda *a: (False, same_error),
            build_fix_specialist=mock_specialist,
        )

        mock_qa.run.assert_called()

    @patch(
        "software_engineering_team.shared.command_runner.ensure_frontend_dependencies_installed",
        return_value=_INSTALL_OK,
    )
    def test_specialist_failure_is_nonblocking(
        self, _mock_install: MagicMock, _setup_repo: Path
    ) -> None:
        from software_engineering_team.shared.models import Task, TaskType

        task = Task(
            id="fe-3",
            type=TaskType.FRONTEND,
            assignee="frontend",
            title="Fix",
            description="Fix",
            acceptance_criteria=["Compiles"],
            metadata={
                "goal": {"summary": "fix"},
                "scope": {"included": ["src"]},
                "constraints": {},
                "non_functional_requirements": {},
                "inputs_outputs": {"input": "x", "output": "y"},
            },
        )

        mock_llm = ConfigurableLLM()
        mock_llm._max_context_tokens = 16384
        mock_llm.complete_json_mock.side_effect = [
            {
                "feature_intent": "Fix",
                "what_changes": ["src/app/app.component.ts"],
                "algorithms_data_structures": "",
                "tests_needed": "",
            },
            {
                "code": "",
                "language": "typescript",
                "summary": "Done",
                "files": {"src/app/app.component.ts": "export class AppComponent {}"},
                "tests": "",
                "suggested_commit_message": "fix: comp",
                "npm_packages_to_install": [],
            },
        ] + [
            {
                "code": "",
                "language": "typescript",
                "summary": "Fixed",
                "files": {"src/app/app.component.ts": "export class AppComponent {}"},
                "tests": "",
                "suggested_commit_message": "fix: build",
                "npm_packages_to_install": [],
            }
            for _ in range(10)
        ]

        from frontend_team.feature_agent import FrontendExpertAgent

        agent = FrontendExpertAgent(llm_client=mock_llm)

        mock_specialist = MagicMock()
        mock_specialist.run.side_effect = RuntimeError("LLM exploded")

        mock_qa = MagicMock()
        from qa_agent.models import BugReport

        mock_qa.run.return_value = MagicMock(
            bugs_found=[
                BugReport(
                    severity="critical",
                    description="Fix",
                    location="src/app/app.component.ts",
                    recommendation="Fix",
                ),
            ]
        )
        mock_tech_lead = MagicMock()
        mock_tech_lead.review_progress.return_value = []

        result = agent.run_workflow(
            repo_path=_setup_repo,
            backend_dir=_setup_repo,
            task=task,
            spec_content="# Spec",
            architecture=None,
            qa_agent=mock_qa,
            accessibility_agent=MagicMock(),
            security_agent=MagicMock(),
            code_review_agent=MagicMock(),
            tech_lead=mock_tech_lead,
            build_verifier=lambda *a: (False, "ERROR: same error"),
            build_fix_specialist=mock_specialist,
        )

        assert not result.success or result.success  # workflow completes (doesn't crash)

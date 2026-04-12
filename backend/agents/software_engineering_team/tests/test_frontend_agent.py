"""Tests for Frontend Expert agent."""


import pytest
from frontend_team.feature_agent import FrontendExpertAgent, FrontendInput, FrontendOutput

from llm_service import DummyLLMClient
from software_engineering_team.tests.conftest import ConfigurableLLM


def test_frontend_output_has_npm_packages_to_install() -> None:
    """FrontendOutput includes npm_packages_to_install field with default empty list."""
    out = FrontendOutput(
        code="",
        summary="",
        files={},
        components=[],
        npm_packages_to_install=[],
    )
    assert out.npm_packages_to_install == []


def test_frontend_output_npm_packages_default() -> None:
    """FrontendOutput defaults npm_packages_to_install to empty list."""
    out = FrontendOutput(code="x", summary="y", files={"src/x.ts": "content"})
    assert out.npm_packages_to_install == []


def test_frontend_agent_parses_npm_packages_from_llm() -> None:
    """Frontend agent parses npm_packages_to_install from LLM JSON and includes in output."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Added component",
        "files": {
            "src/app/components/example/example.component.ts": "import { Component } from '@angular/core';\n@Component({ selector: 'app-example', template: '<p>hi</p>' }) export class ExampleComponent {}",
        },
        "components": ["example"],
        "suggested_commit_message": "feat(ui): add example",
        "npm_packages_to_install": ["@ngrx/store", "ngx-toastr"],
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(
        FrontendInput(
            task_description="Add example component",
            requirements="Use NgRx and toastr",
        )
    )
    assert result.npm_packages_to_install == ["@ngrx/store", "ngx-toastr"]


def test_frontend_agent_npm_packages_empty_when_omitted() -> None:
    """When LLM omits npm_packages_to_install, output has empty list."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Added component",
        "files": {
            "src/app/components/example/example.component.ts": "import { Component } from '@angular/core';\n@Component({ selector: 'app-example', template: '<p>hi</p>' }) export class ExampleComponent {}",
        },
        "components": ["example"],
        "suggested_commit_message": "feat(ui): add example",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Add example", requirements=""))
    assert result.npm_packages_to_install == []


def test_frontend_agent_npm_packages_normalizes_non_list() -> None:
    """When LLM returns non-list npm_packages_to_install, it is normalized to list."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Added component",
        "files": {
            "src/app/components/example/example.component.ts": "import { Component } from '@angular/core';\n@Component({ selector: 'app-example', template: '<p>hi</p>' }) export class ExampleComponent {}",
        },
        "components": ["example"],
        "suggested_commit_message": "feat(ui): add example",
        "npm_packages_to_install": "ngx-toastr",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Add example", requirements=""))
    assert result.npm_packages_to_install == ["ngx-toastr"]


def test_frontend_agent_with_dummy_llm() -> None:
    """Frontend agent runs with DummyLLMClient and returns valid output."""
    llm = DummyLLMClient()
    agent = FrontendExpertAgent(llm_client=llm)
    result = agent.run(
        FrontendInput(
            task_description="Create user form component",
            requirements="Angular reactive forms",
        )
    )
    assert result is not None
    assert isinstance(result, FrontendOutput)
    assert result.files
    assert "src/" in next(iter(result.files))
    assert result.npm_packages_to_install == []


def test_frontend_agent_rejects_segment_too_long() -> None:
    """Agent rejects path segments longer than 30 chars."""
    mock_llm = ConfigurableLLM()
    long_name = "a" * 35
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {
            f"src/app/{long_name}.component.ts": "content",
            "src/app/short.component.ts": "import { Component } from '@angular/core';\n@Component({selector: 'app-short', template: 'x'}) export class Short {}",
        },
        "components": ["short"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Add", requirements=""))
    assert long_name not in str(result.files.keys())
    assert "src/app/short.component.ts" in result.files


def test_frontend_agent_rejects_sentence_like_name() -> None:
    """Agent rejects path segments that look like sentences (4+ hyphenated words)."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {
            "src/app/one-two-three-four-five.component.ts": "content",
            "src/app/task-list.component.ts": "import { Component } from '@angular/core';\n@Component({selector: 'app-task-list', template: 'x'}) export class TaskList {}",
        },
        "components": ["task-list"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Add", requirements=""))
    assert "one-two-three-four-five" not in str(result.files.keys())
    assert "src/app/task-list.component.ts" in result.files


def test_frontend_agent_rejects_filler_words_in_path() -> None:
    """Agent rejects path segments with filler words like -the-, -with-."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {
            "src/app/component-with-the-thing.component.ts": "content",
            "src/app/thing.component.ts": "import { Component } from '@angular/core';\n@Component({selector: 'app-thing', template: 'x'}) export class Thing {}",
        },
        "components": ["thing"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Add", requirements=""))
    assert "component-with-the-thing" not in str(result.files.keys())
    assert "src/app/thing.component.ts" in result.files


def test_frontend_agent_rejects_verb_prefix_in_path() -> None:
    """Agent rejects path segments starting with verbs like implement-."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {
            "src/app/implement-user-form.component.ts": "content",
            "src/app/user-form.component.ts": "import { Component } from '@angular/core';\n@Component({selector: 'app-user-form', template: 'x'}) export class UserForm {}",
        },
        "components": ["user-form"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Add", requirements=""))
    assert "implement-user-form" not in str(result.files.keys())
    assert "src/app/user-form.component.ts" in result.files


def test_frontend_agent_rejects_path_not_under_src() -> None:
    """Agent rejects files not under src/."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {
            "app/x.component.ts": "content",
            "src/app/valid.component.ts": "import { Component } from '@angular/core';\n@Component({selector: 'app-valid', template: 'x'}) export class Valid {}",
        },
        "components": ["valid"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Add component", requirements=""))
    assert "app/x.component.ts" not in result.files
    assert "src/app/valid.component.ts" in result.files


def test_frontend_agent_allows_known_angular_root_config_files() -> None:
    """Agent allows valid Angular root config files like tsconfig.spec.json."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Update tsconfig and component",
        "files": {
            "tsconfig.spec.json": '{"extends":"./tsconfig.json"}',
            "src/app/valid.component.ts": "import { Component } from '@angular/core';\n@Component({selector: 'app-valid', template: 'x'}) export class Valid {}",
        },
        "components": ["valid"],
        "suggested_commit_message": "chore: adjust test tsconfig",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Update test config", requirements=""))
    assert "tsconfig.spec.json" in result.files
    assert "src/app/valid.component.ts" in result.files


def test_frontend_agent_rejects_bad_extension() -> None:
    """Agent rejects non-browser file extensions."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {
            "src/app/foo.py": "print('hi')",
            "src/app/bar.component.ts": "import { Component } from '@angular/core';\n@Component({selector: 'app-bar', template: 'x'}) export class Bar {}",
        },
        "components": ["bar"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Add", requirements=""))
    assert "src/app/foo.py" not in result.files
    assert "src/app/bar.component.ts" in result.files


def test_frontend_agent_rejects_empty_content() -> None:
    """Agent rejects files with empty content."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {
            "src/app/empty.component.ts": "",
            "src/app/ok.component.ts": "import { Component } from '@angular/core';\n@Component({selector: 'app-ok', template: 'x'}) export class Ok {}",
        },
        "components": ["ok"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Add", requirements=""))
    assert "src/app/empty.component.ts" not in result.files
    assert "src/app/ok.component.ts" in result.files


def test_frontend_agent_with_architecture() -> None:
    """Agent includes architecture in context when provided."""
    from software_engineering_team.shared.models import ArchitectureComponent, SystemArchitecture

    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {"src/app/x.component.ts": "content"},
        "components": ["x"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    arch = SystemArchitecture(
        overview="Test arch",
        components=[ArchitectureComponent(name="UserService", type="frontend")],
    )
    agent.run(
        FrontendInput(
            task_description="Add component",
            requirements="",
            architecture=arch,
        )
    )
    call_args = mock_llm.complete_json_mock.call_args[0][0]
    assert "Architecture" in call_args
    assert "Test arch" in call_args
    assert "UserService" in call_args


def test_frontend_agent_with_security_issues() -> None:
    """Agent includes security_issues in context when provided."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {"src/app/x.component.ts": "content"},
        "components": ["x"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    agent.run(
        FrontendInput(
            task_description="Fix vulnerability",
            requirements="",
            security_issues=[
                {
                    "severity": "high",
                    "category": "xss",
                    "description": "Vuln",
                    "recommendation": "Fix",
                    "location": "x",
                }
            ],
        )
    )
    call_args = mock_llm.complete_json_mock.call_args[0][0]
    assert "Security issues" in call_args


def test_frontend_agent_with_accessibility_issues() -> None:
    """Agent includes accessibility_issues in context when provided."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {"src/app/x.component.ts": "content"},
        "components": ["x"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    agent.run(
        FrontendInput(
            task_description="Fix a11y",
            requirements="",
            accessibility_issues=[
                {
                    "severity": "medium",
                    "wcag_criterion": "1.1.1",
                    "description": "Missing alt",
                    "recommendation": "Add alt",
                    "location": "x",
                }
            ],
        )
    )
    call_args = mock_llm.complete_json_mock.call_args[0][0]
    assert "Accessibility" in call_args


def test_frontend_agent_with_code_review_issues() -> None:
    """Agent includes code_review_issues in context when provided."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {"src/app/x.component.ts": "content"},
        "components": ["x"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    agent.run(
        FrontendInput(
            task_description="Address review",
            requirements="",
            code_review_issues=[
                {
                    "severity": "medium",
                    "description": "Refactor",
                    "suggestion": "Simplify",
                    "file_path": "x.ts",
                }
            ],
        )
    )
    call_args = mock_llm.complete_json_mock.call_args[0][0]
    assert "Code review" in call_args


def test_frontend_agent_includes_problem_solving_header_when_issues_present() -> None:
    """When code_review_issues are present, prompt includes PROBLEM-SOLVING MODE header."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Fixed",
        "files": {"src/app/x.component.ts": "content"},
        "components": ["x"],
        "suggested_commit_message": "fix: resolve",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    agent.run(
        FrontendInput(
            task_description="Fix ng build",
            requirements="",
            framework_target="angular",
            code_review_issues=[
                {
                    "severity": "critical",
                    "category": "build",
                    "description": "NG8002 formGroup",
                    "suggestion": "Add ReactiveFormsModule",
                    "file_path": "x.component.ts",
                },
            ],
        )
    )
    prompt = mock_llm.complete_json_mock.call_args[0][0]
    assert "PROBLEM-SOLVING MODE" in prompt
    assert "Frontend / Angular" in prompt
    assert "code review issues" in prompt
    assert "NG8002" in prompt or "ReactiveFormsModule" in prompt
    assert "Add ReactiveFormsModule" in prompt
    assert "error details above" not in prompt


def test_frontend_agent_no_problem_solving_header_when_no_issues() -> None:
    """When no issues are present, prompt does not include PROBLEM-SOLVING MODE header."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Added",
        "files": {"src/app/x.component.ts": "content"},
        "components": ["x"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    agent.run(FrontendInput(task_description="Add component", requirements=""))
    prompt = mock_llm.complete_json_mock.call_args[0][0]
    assert "PROBLEM-SOLVING MODE" not in prompt


def test_frontend_agent_logs_llm_prompt(caplog: pytest.LogCaptureFixture) -> None:
    """Frontend agent logs LLM call metadata before each LLM call."""
    import logging

    caplog.set_level(logging.INFO)
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Done",
        "files": {"src/app/x.component.ts": "content"},
        "components": ["x"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    agent.run(FrontendInput(task_description="Add component", requirements=""))
    assert any(
        "LLM call" in rec.message and "agent=Frontend" in rec.message for rec in caplog.records
    )
    assert any("mode=initial" in rec.message for rec in caplog.records)


def test_frontend_agent_logs_problem_solving_context_and_header_when_issues_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Frontend agent logs problem-solving context and header when issues are present."""
    import logging

    caplog.set_level(logging.INFO)
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Fixed",
        "files": {"src/app/x.component.ts": "content"},
        "components": ["x"],
        "suggested_commit_message": "fix: resolve",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    agent.run(
        FrontendInput(
            task_description="Fix build",
            requirements="",
            code_review_issues=[
                {
                    "severity": "critical",
                    "category": "build",
                    "description": "ng build failed",
                    "suggestion": "Fix Angular errors",
                    "file_path": "src/app/x.component.ts",
                },
            ],
        )
    )
    assert any("Frontend problem-solving context" in rec.message for rec in caplog.records)
    assert any("Frontend problem-solving header for LLM" in rec.message for rec in caplog.records)
    assert any("mode=problem_solving" in rec.message for rec in caplog.records)


def test_frontend_agent_no_problem_solving_logs_when_no_issues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Frontend agent does not log problem-solving context/header when no issues."""
    import logging

    caplog.set_level(logging.INFO)
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Done",
        "files": {"src/app/x.component.ts": "content"},
        "components": ["x"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    agent.run(FrontendInput(task_description="Add component", requirements=""))
    assert not any("Frontend problem-solving context" in rec.message for rec in caplog.records)
    assert not any(
        "Frontend problem-solving header for LLM" in rec.message for rec in caplog.records
    )


def test_frontend_agent_with_qa_issues() -> None:
    """Agent includes qa_issues in context when provided."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {"src/app/x.component.ts": "content"},
        "components": ["x"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    agent.run(
        FrontendInput(
            task_description="Fix bugs",
            requirements="",
            qa_issues=[
                {
                    "severity": "high",
                    "description": "Bug",
                    "recommendation": "Fix it",
                    "location": "x.ts",
                }
            ],
        )
    )
    call_args = mock_llm.complete_json_mock.call_args[0][0]
    assert "QA issues" in call_args
    assert "Bug" in call_args


def test_frontend_agent_clarification_requests_non_list_normalized() -> None:
    """Agent normalizes non-list clarification_requests to list."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Need info",
        "files": {},
        "components": [],
        "suggested_commit_message": "",
        "needs_clarification": True,
        "clarification_requests": "Single question",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Vague", requirements=""))
    assert result.needs_clarification
    assert "Single question" in result.clarification_requests


def test_frontend_agent_needs_clarification() -> None:
    """Agent returns needs_clarification when LLM says so."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Need more info",
        "files": {},
        "components": [],
        "suggested_commit_message": "",
        "needs_clarification": True,
        "clarification_requests": ["What API format?"],
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Vague task", requirements=""))
    assert result.needs_clarification
    assert "What API format?" in result.clarification_requests


def test_frontend_agent_all_files_rejected_raises_llm_permanent_error() -> None:
    """When all files rejected by validation, agent raises LLMPermanentError (fail fast)."""
    from llm_service import LLMPermanentError

    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "import { Component } from '@angular/core';\n@Component({selector: 'app-x', template: 'x'}) export class X {}",
        "summary": "Test",
        "files": {"wrong/path.ts": "content"},
        "components": ["x"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    with pytest.raises(LLMPermanentError, match="all were rejected"):
        agent.run(FrontendInput(task_description="Add", requirements=""))


def test_frontend_agent_unescapes_newlines_in_files() -> None:
    """Agent unescapes \\n in file contents."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Test",
        "files": {"src/app/x.component.ts": "line1\\nline2"},
        "components": ["x"],
        "suggested_commit_message": "feat: add",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    result = agent.run(FrontendInput(task_description="Add", requirements=""))
    assert result.files["src/app/x.component.ts"] == "line1\nline2"


def test_read_repo_code_excludes_node_modules_and_dist(tmp_path):
    """_read_repo_code excludes node_modules, dist, and .angular so code review stays under body limit."""
    from pathlib import Path

    from frontend_team.feature_agent.agent import _read_repo_code

    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "node_modules" / "foo").mkdir(parents=True)
    (tmp_path / "dist").mkdir(parents=True)
    (tmp_path / ".angular").mkdir(parents=True)

    (tmp_path / "src" / "app" / "app.ts").write_text("// app source", encoding="utf-8")
    (tmp_path / "node_modules" / "foo" / "bar.ts").write_text(
        "// node_modules content", encoding="utf-8"
    )
    (tmp_path / "dist" / "main.js").write_text("// dist is not .ts", encoding="utf-8")
    (tmp_path / ".angular" / "cache").write_text("cache", encoding="utf-8")

    result = _read_repo_code(Path(tmp_path), [".ts", ".tsx", ".html", ".scss"])

    assert "// app source" in result
    assert "// node_modules content" not in result
    assert "node_modules" not in result
    assert "dist" not in result
    assert ".angular" not in result
    assert len(result) < 500


def test_frontend_plan_task_returns_plan_markdown() -> None:
    """_plan_task parses LLM JSON and returns plan markdown."""
    from software_engineering_team.shared.models import Task, TaskType

    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "feature_intent": "Add task list component",
        "what_changes": ["src/app/components/task-list/"],
        "algorithms_data_structures": "RxJS BehaviorSubject for list state",
        "tests_needed": "task-list.component.spec.ts",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    task = Task(
        id="f1",
        type=TaskType.FRONTEND,
        assignee="frontend",
        title="Add task list",
        description="Implement task list component",
    )
    plan_text = agent._plan_task(
        task=task,
        existing_code="# No code",
        spec_content="",
        architecture=None,
    )
    assert plan_text
    assert "Add task list component" in plan_text
    assert "task-list" in plan_text
    assert "BehaviorSubject" in plan_text


def test_frontend_run_injects_task_plan_and_follow_instruction_into_prompt() -> None:
    """When task_plan is set, run() injects Implementation plan and follow-plan instruction into prompt."""
    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
        "code": "",
        "summary": "Done",
        "files": {
            "src/app/components/foo/foo.component.ts": "import { Component } from '@angular/core';\n@Component({selector: 'app-foo', template: 'x'}) export class FooComponent {}",
        },
        "components": ["foo"],
        "suggested_commit_message": "feat: add foo",
    }
    agent = FrontendExpertAgent(llm_client=mock_llm)
    plan_content = "**Feature intent:** Add foo\n**What changes:** src/app/components/foo/"
    agent.run(
        FrontendInput(
            task_description="Add foo component",
            requirements="",
            task_plan=plan_content,
        )
    )
    prompt = mock_llm.complete_json_mock.call_args[0][0]
    assert "IMPLEMENTATION PLAN (follow this)" in prompt
    assert "Implement the task strictly according to" in prompt
    assert "realize every item under 'What changes' and 'Tests needed'" in prompt
    assert "Add foo" in prompt

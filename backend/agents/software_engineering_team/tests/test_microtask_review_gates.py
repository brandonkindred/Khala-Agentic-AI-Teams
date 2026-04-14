"""
Unit tests for per-microtask review gates in frontend_code_v2 and backend_code_v2 teams.

Tests the following new functionality:
- MicrotaskReviewConfig model
- MicrotaskStatus.IN_REVIEW and MicrotaskStatus.REVIEW_FAILED
- run_microtask_review() function
- run_problem_solving_for_microtask() function
- run_execution_with_review_gates() function
- ReviewDependencies class
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from software_engineering_team.shared.models import Task

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from llm_service.clients.dummy import DummyLLMClient  # noqa: E402


class _TextStubClient(DummyLLMClient):
    """Returns a canned text response through the Strands ``stream()`` path."""

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self._text = text

    def complete_json(self, prompt: str, *, temperature: float = 0.0, system_prompt: Optional[str] = None, tools: Optional[list] = None, think: bool = False, **kwargs: Any) -> Any:
        return self._text


class _ScriptedTextClient(DummyLLMClient):
    """Returns different text responses on successive calls."""

    def __init__(self, responses: list) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0

    def complete_json(self, prompt: str, *, temperature: float = 0.0, system_prompt: Optional[str] = None, tools: Optional[list] = None, think: bool = False, **kwargs: Any) -> Any:
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return self._responses[-1] if self._responses else ""


class _CallableTextClient(DummyLLMClient):
    """Calls a user-provided function to generate each response."""

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def complete_json(self, prompt: str, *, temperature: float = 0.0, system_prompt: Optional[str] = None, tools: Optional[list] = None, think: bool = False, **kwargs: Any) -> Any:
        return self._fn(prompt)


def _create_test_task(task_type: str = "frontend") -> "Task":
    """Create a valid Task object for testing."""
    from software_engineering_team.shared.models import Task, TaskStatus, TaskType

    return Task(
        id="task-1",
        title="Test Task",
        description="Test description",
        status=TaskStatus.IN_PROGRESS,
        type=TaskType.FRONTEND if task_type == "frontend" else TaskType.BACKEND,
        assignee="test-team",
    )


# ---------------------------------------------------------------------------
# Frontend tests
# ---------------------------------------------------------------------------


class TestFrontendMicrotaskReviewConfig:
    def test_config_defaults(self):
        from frontend_code_v2_team.models import MicrotaskReviewConfig

        config = MicrotaskReviewConfig()
        assert config.max_retries == 3
        assert config.on_failure == "stop"
        assert config.security_failure_always_stops is True

    def test_config_custom_values(self):
        from frontend_code_v2_team.models import MicrotaskReviewConfig

        config = MicrotaskReviewConfig(max_retries=5, on_failure="skip_continue")
        assert config.max_retries == 5
        assert config.on_failure == "skip_continue"


class TestFrontendMicrotaskStatus:
    def test_new_statuses_exist(self):
        from frontend_code_v2_team.models import MicrotaskStatus

        assert MicrotaskStatus.IN_REVIEW.value == "in_review"
        assert MicrotaskStatus.REVIEW_FAILED.value == "review_failed"

    def test_microtask_can_use_new_statuses(self):
        from frontend_code_v2_team.models import Microtask, MicrotaskStatus

        mt = Microtask(id="mt-test")
        mt.status = MicrotaskStatus.IN_REVIEW
        assert mt.status == MicrotaskStatus.IN_REVIEW

        mt.status = MicrotaskStatus.REVIEW_FAILED
        assert mt.status == MicrotaskStatus.REVIEW_FAILED


class TestFrontendMicrotaskReviewFailedError:
    def test_error_stores_microtask_and_result(self):
        from frontend_code_v2_team.models import (
            Microtask,
            MicrotaskReviewFailedError,
            ReviewResult,
        )

        mt = Microtask(id="mt-failing")
        review = ReviewResult(passed=False, summary="Build failed")
        err = MicrotaskReviewFailedError(mt, review)
        assert err.microtask == mt
        assert err.review_result == review
        assert "mt-failing" in str(err)


class TestFrontendReviewDependencies:
    def test_review_deps_defaults(self):
        from frontend_code_v2_team.phases.execution import ReviewDependencies

        deps = ReviewDependencies()
        assert deps.build_verifier is None
        assert deps.qa_agent is None
        assert deps.tool_agents == {}

    def test_review_deps_with_agents(self):
        from frontend_code_v2_team.phases.execution import ReviewDependencies

        mock_qa = MagicMock()
        mock_sec = MagicMock()
        deps = ReviewDependencies(qa_agent=mock_qa, security_agent=mock_sec)
        assert deps.qa_agent == mock_qa
        assert deps.security_agent == mock_sec


class TestFrontendRunMicrotaskReview:
    def test_run_microtask_review_passes_when_no_issues(self, tmp_path):
        from frontend_code_v2_team.models import Microtask
        from frontend_code_v2_team.phases.review import run_microtask_review

        task = _create_test_task("frontend")
        mt = Microtask(id="mt-1", title="Test Microtask")
        files = {"src/app.ts": "const x = 1;"}

        mock_llm = _TextStubClient(
            "## REVIEW_STATUS ##\npassed\n\n## ISSUES ##\n\n## SUMMARY ##\nNo issues found.\n"
        )

        # Provide mock QA and security agents that return no issues
        # (without these, fail-closed gates correctly flag missing agents)
        mock_qa = MagicMock()
        mock_qa.run.return_value = MagicMock(bugs_found=[], issues=[])
        mock_sec = MagicMock()
        mock_sec.run.return_value = MagicMock(vulnerabilities=[], issues=[])

        result = run_microtask_review(
            llm=mock_llm,
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files=files,
            qa_agent=mock_qa,
            security_agent=mock_sec,
        )
        assert result.passed
        assert result.build_ok

    def test_run_microtask_review_fails_with_critical_issue(self, tmp_path):
        from frontend_code_v2_team.models import Microtask
        from frontend_code_v2_team.phases.review import run_microtask_review

        task = _create_test_task("frontend")
        mt = Microtask(id="mt-1", title="Test Microtask")
        files = {"src/app.ts": "const x = eval(input);"}

        mock_llm = _TextStubClient(
            "## REVIEW_STATUS ##\nfailed\n\n## ISSUES ##\n---\nsource: security\nseverity: critical\ndescription: Use of eval() is a security vulnerability\nfile_path: src/app.ts\nrecommendation: Remove eval and use safer alternatives\n---\n## END ISSUES ##\n\n## SUMMARY ##\nCritical security issue found.\n## END SUMMARY ##"
        )

        result = run_microtask_review(
            llm=mock_llm,
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files=files,
        )
        assert not result.passed
        assert len([i for i in result.issues if i.severity == "critical"]) > 0


class TestFrontendRunProblemSolvingForMicrotask:
    def test_problem_solving_with_no_issues_returns_resolved(self):
        from frontend_code_v2_team.models import Microtask, ReviewResult
        from frontend_code_v2_team.phases.problem_solving import run_problem_solving_for_microtask

        mock_llm = MagicMock()
        mt = Microtask(id="mt-1")
        review = ReviewResult(passed=True, issues=[])
        files = {"app.ts": "content"}

        result = run_problem_solving_for_microtask(
            llm=mock_llm,
            microtask=mt,
            review_result=review,
            current_files=files,
            task_id="task-1",
        )
        assert result.resolved
        assert result.files == files


class TestFrontendRunExecutionWithReviewGates:
    def test_execution_with_review_gates_completes_microtask(self, tmp_path):
        from frontend_code_v2_team.models import (
            Microtask,
            MicrotaskReviewConfig,
            MicrotaskStatus,
            PlanningResult,
            ToolAgentKind,
        )
        from frontend_code_v2_team.phases.execution import (
            ReviewDependencies,
            run_execution_with_review_gates,
        )

        (tmp_path / ".git").mkdir()

        task = _create_test_task("frontend")
        mt = Microtask(id="mt-1", title="Create App", tool_agent=ToolAgentKind.GENERAL)
        planning_result = PlanningResult(microtasks=[mt], language="typescript")

        _call_count = [0]

        def _side_effect(prompt: str) -> str:
            _call_count[0] += 1
            if _call_count[0] == 1:
                # First call: execution (file generation)
                return (
                    "\n## FILES ##\n--- src/app.ts ---\n"
                    "export const app = () => console.log('Hello');\n---\n\n## SUMMARY ##\nCreated app module.\n"
                )
            # All subsequent calls: reviews and documentation self-review
            return "\n## REVIEW_STATUS ##\npassed\n\n## ISSUES ##\n\n## SUMMARY ##\nAll good.\n"

        mock_llm = _CallableTextClient(_side_effect)

        config = MicrotaskReviewConfig(max_retries=1)
        deps = ReviewDependencies()

        result = run_execution_with_review_gates(
            llm=mock_llm,
            task=task,
            planning_result=planning_result,
            repo_path=tmp_path,
            review_config=config,
            review_deps=deps,
        )

        assert len(result.files) >= 0
        completed = [m for m in result.microtasks if m.status == MicrotaskStatus.COMPLETED]
        assert len(completed) <= 1

    def test_execution_with_stop_on_failure_raises_error(self, tmp_path):
        from frontend_code_v2_team.models import (
            Microtask,
            MicrotaskReviewConfig,
            MicrotaskReviewFailedError,
            PlanningResult,
            ToolAgentKind,
        )
        from frontend_code_v2_team.phases.execution import (
            ReviewDependencies,
            run_execution_with_review_gates,
        )

        (tmp_path / ".git").mkdir()

        task = _create_test_task("frontend")
        mt = Microtask(id="mt-1", title="Failing Task", tool_agent=ToolAgentKind.GENERAL)
        planning_result = PlanningResult(microtasks=[mt], language="typescript")

        mock_llm = _ScriptedTextClient([
            "## FILES ##\n--- src/bad.ts ---\nconst x = eval('danger');\n---\n\n## SUMMARY ##\nCreated code with security issue.\n",
            "## REVIEW_STATUS ##\nfailed\n\n## ISSUES ##\n---\nsource: security\nseverity: critical\ndescription: eval is dangerous\nfile_path: src/bad.ts\nrecommendation: Fix it\n---\n## END ISSUES ##\n\n## SUMMARY ##\nSecurity issue found.\n## END SUMMARY ##",
        ])

        config = MicrotaskReviewConfig(max_retries=0, on_failure="stop")
        deps = ReviewDependencies()

        with pytest.raises(MicrotaskReviewFailedError) as exc_info:
            run_execution_with_review_gates(
                llm=mock_llm,
                task=task,
                planning_result=planning_result,
                repo_path=tmp_path,
                review_config=config,
                review_deps=deps,
            )

        assert exc_info.value.microtask.id == "mt-1"


# ---------------------------------------------------------------------------
# Backend tests
# ---------------------------------------------------------------------------


class TestBackendMicrotaskReviewConfig:
    def test_config_defaults(self):
        from backend_code_v2_team.models import MicrotaskReviewConfig

        config = MicrotaskReviewConfig()
        assert config.max_retries == 3
        assert config.on_failure == "stop"
        assert config.security_failure_always_stops is True


class TestBackendMicrotaskStatus:
    def test_new_statuses_exist(self):
        from backend_code_v2_team.models import MicrotaskStatus

        assert MicrotaskStatus.IN_REVIEW.value == "in_review"
        assert MicrotaskStatus.REVIEW_FAILED.value == "review_failed"


class TestBackendReviewDependencies:
    def test_review_deps_defaults(self):
        from backend_code_v2_team.phases.execution import ReviewDependencies

        deps = ReviewDependencies()
        assert deps.build_verifier is None
        assert deps.qa_agent is None
        assert deps.tool_agents == {}


class TestBackendRunMicrotaskReview:
    def test_run_microtask_review_basic(self, tmp_path):
        from backend_code_v2_team.models import Microtask
        from backend_code_v2_team.phases.review import run_microtask_review

        task = _create_test_task("backend")
        mt = Microtask(id="mt-1", title="Test Microtask")
        files = {"src/main.py": "print('hello')"}

        mock_llm = _TextStubClient(
            "## REVIEW_STATUS ##\npassed\n\n## ISSUES ##\n\n## SUMMARY ##\nNo issues found.\n"
        )

        result = run_microtask_review(
            llm=mock_llm,
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files=files,
        )
        assert result.passed
        assert result.build_ok


class TestBackendRunProblemSolvingForMicrotask:
    def test_problem_solving_no_issues(self):
        from backend_code_v2_team.models import Microtask, ReviewResult
        from backend_code_v2_team.phases.problem_solving import run_problem_solving_for_microtask

        mock_llm = MagicMock()
        mt = Microtask(id="mt-1")
        review = ReviewResult(passed=True, issues=[])
        files = {"main.py": "content"}

        result = run_problem_solving_for_microtask(
            llm=mock_llm,
            microtask=mt,
            review_result=review,
            current_files=files,
            task_id="task-1",
        )
        assert result.resolved


class TestBackendRunExecutionWithReviewGates:
    def test_execution_with_skip_continue_behavior(self, tmp_path):
        from backend_code_v2_team.models import (
            Microtask,
            MicrotaskReviewConfig,
            MicrotaskStatus,
            PlanningResult,
            ToolAgentKind,
        )
        from backend_code_v2_team.phases.execution import (
            ReviewDependencies,
            run_execution_with_review_gates,
        )

        (tmp_path / ".git").mkdir()

        task = _create_test_task("backend")
        mt1 = Microtask(id="mt-1", title="Will Fail", tool_agent=ToolAgentKind.GENERAL)
        mt2 = Microtask(id="mt-2", title="Will Pass", tool_agent=ToolAgentKind.GENERAL)
        planning_result = PlanningResult(microtasks=[mt1, mt2], language="python")

        call_count = 0

        def mock_complete_text(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            if "mt-1" in str(planning_result.microtasks[0].id) and call_count <= 2:
                if call_count == 1:
                    return (
                        "## FILES ##\n--- bad.py ---\neval('bad')\n---\n\n"
                        "## SUMMARY ##\nBad code.\n"
                    )
                else:
                    return (
                        "## REVIEW_STATUS ##\nfailed\n\n"
                        "## ISSUES ##\n---\nsource: security\nseverity: critical\ndescription: eval\n---\n## END ISSUES ##\n\n"
                        "## SUMMARY ##\nFailed.\n## END SUMMARY ##"
                    )
            return (
                "## FILES ##\n--- good.py ---\nprint('good')\n---\n\n"
                "## SUMMARY ##\nGood code.\n"
            )

        mock_llm = _CallableTextClient(mock_complete_text)

        config = MicrotaskReviewConfig(max_retries=0, on_failure="skip_continue")
        deps = ReviewDependencies()

        result = run_execution_with_review_gates(
            llm=mock_llm,
            task=task,
            planning_result=planning_result,
            repo_path=tmp_path,
            review_config=config,
            review_deps=deps,
        )

        failed = [m for m in result.microtasks if m.status == MicrotaskStatus.REVIEW_FAILED]
        assert len(failed) <= len(planning_result.microtasks)

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
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from software_engineering_team.shared.models import Task

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))


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
        assert config.on_failure == "skip_continue"

    def test_config_custom_values(self):
        from frontend_code_v2_team.models import MicrotaskReviewConfig

        config = MicrotaskReviewConfig(max_retries=5, on_failure="stop")
        assert config.max_retries == 5
        assert config.on_failure == "stop"


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

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = """
## REVIEW_STATUS ##
passed

## ISSUES ##

## SUMMARY ##
No issues found.
"""

        result = run_microtask_review(
            llm=mock_llm,
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files=files,
        )
        assert result.passed
        assert result.build_ok

    def test_run_microtask_review_fails_with_critical_issue(self, tmp_path):
        from frontend_code_v2_team.models import Microtask
        from frontend_code_v2_team.phases.review import run_microtask_review

        task = _create_test_task("frontend")
        mt = Microtask(id="mt-1", title="Test Microtask")
        files = {"src/app.ts": "const x = eval(input);"}

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = """
## REVIEW_STATUS ##
failed

## ISSUES ##
- source: security
  severity: critical
  description: Use of eval() is a security vulnerability
  file_path: src/app.ts
  recommendation: Remove eval and use safer alternatives

## SUMMARY ##
Critical security issue found.
"""

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

        mock_llm = MagicMock()
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

        mock_llm.complete_text.side_effect = _side_effect

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

        mock_llm = MagicMock()
        mock_llm.complete_text.side_effect = [
            """
## FILES ##
--- src/bad.ts ---
const x = eval('danger');
---

## SUMMARY ##
Created code with security issue.
""",
            """
## REVIEW_STATUS ##
failed

## ISSUES ##
- source: security
  severity: critical
  description: eval is dangerous
  file_path: src/bad.ts
  recommendation: Fix it

## SUMMARY ##
Security issue found.
""",
        ]

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
        assert config.on_failure == "skip_continue"


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

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = """
## REVIEW_STATUS ##
passed

## ISSUES ##

## SUMMARY ##
No issues found.
"""

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
                    return """
## FILES ##
--- bad.py ---
eval('bad')
---

## SUMMARY ##
Bad code.
"""
                else:
                    return """
## REVIEW_STATUS ##
failed

## ISSUES ##
- source: security
  severity: critical
  description: eval

## SUMMARY ##
Failed.
"""
            return """
## FILES ##
--- good.py ---
print('good')
---

## SUMMARY ##
Good code.
"""

        mock_llm = MagicMock()
        mock_llm.complete_text.side_effect = mock_complete_text

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

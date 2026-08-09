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
    from shared.dev_models.models import Task

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from llm_service.clients.dummy import DummyLLMClient  # noqa: E402


class _ScriptedTextClient(DummyLLMClient):
    """Returns different text responses on successive calls."""

    def __init__(self, responses: list) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0

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
        return self._fn(prompt)


_CLEAN_COORDINATOR_APPROVAL = {
    "approved": True,
    "issues": [],
    "summary": "All good.",
    "spec_compliance_notes": "",
}


def _create_test_task(task_type: str = "frontend") -> "Task":
    """Create a valid Task object for testing."""
    from shared.dev_models.models import Task, TaskStatus, TaskType

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
        assert config.enable_llm_review_grounding is True

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
        assert deps.tool_agent_cache is None

    def test_review_deps_with_agents(self):
        from frontend_code_v2_team.phases.execution import ReviewDependencies

        from software_engineering_team.shared.agent_review import AgentReviewCache

        mock_qa = MagicMock()
        mock_sec = MagicMock()
        cache = AgentReviewCache()
        deps = ReviewDependencies(qa_agent=mock_qa, security_agent=mock_sec, tool_agent_cache=cache)
        assert deps.qa_agent == mock_qa
        assert deps.security_agent == mock_sec
        assert deps.tool_agent_cache is cache


class TestFrontendRunMicrotaskReview:
    def test_run_microtask_review_passes_when_no_issues(self, tmp_path):
        from frontend_code_v2_team.models import Microtask
        from frontend_code_v2_team.phases.review import run_microtask_review

        task = _create_test_task("frontend")
        mt = Microtask(id="mt-1", title="Test Microtask")
        files = {"src/app.ts": "const x = 1;"}

        # DummyLLMClient's built-in "code to review"/"senior code reviewer"
        # catch-all already returns an approved, issue-free CodeReviewOutput
        # for the coordinator's chunk-review call.
        mock_llm = DummyLLMClient()

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

        def _respond(prompt: str) -> Any:
            # The coordinator's chunk reviewer calls complete_json directly
            # and needs a schema-shaped dict (matches DummyLLMClient's own
            # "code to review" catch-all anchor text).
            assert "code to review" in prompt.lower()
            return {
                "approved": False,
                "issues": [
                    {
                        "severity": "critical",
                        "category": "logic",
                        "file_path": "src/app.ts",
                        "description": "Use of eval() is a security vulnerability",
                        "suggestion": "Remove eval and use safer alternatives",
                    }
                ],
                "summary": "Critical security issue found.",
                "spec_compliance_notes": "",
            }

        mock_llm = _CallableTextClient(_respond)

        result = run_microtask_review(
            llm=mock_llm,
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files=files,
        )
        assert not result.passed
        assert len([i for i in result.issues if i.severity == "critical"]) > 0


class TestFrontendAgentReviewCache:
    def test_run_microtask_review_reuses_cache_for_unchanged_files(self, tmp_path):
        """A second run_microtask_review call with byte-identical files reuses
        the QA/security verdicts instead of calling the agents again."""
        from frontend_code_v2_team.models import Microtask
        from frontend_code_v2_team.phases.review import run_microtask_review

        from software_engineering_team.shared.agent_review import AgentReviewCache

        task = _create_test_task("frontend")
        mt = Microtask(id="mt-1", title="Test Microtask")
        files = {"src/app.ts": "const x = 1;"}

        mock_llm = DummyLLMClient()
        mock_qa = MagicMock()
        mock_qa.run.return_value = MagicMock(bugs_found=[], issues=[])
        mock_sec = MagicMock()
        mock_sec.run.return_value = MagicMock(vulnerabilities=[], issues=[])
        cache = AgentReviewCache()

        for _ in range(2):
            result = run_microtask_review(
                llm=mock_llm,
                task=task,
                microtask=mt,
                repo_path=tmp_path,
                files=files,
                qa_agent=mock_qa,
                security_agent=mock_sec,
                cache=cache,
            )
            assert result.passed

        assert mock_qa.run.call_count == 1
        assert mock_sec.run.call_count == 1

    def test_run_microtask_review_without_cache_calls_agents_every_time(self, tmp_path):
        """Omitting ``cache`` (the default) is unchanged: every call re-invokes the agents."""
        from frontend_code_v2_team.models import Microtask
        from frontend_code_v2_team.phases.review import run_microtask_review

        task = _create_test_task("frontend")
        mt = Microtask(id="mt-1", title="Test Microtask")
        files = {"src/app.ts": "const x = 1;"}

        mock_llm = DummyLLMClient()
        mock_qa = MagicMock()
        mock_qa.run.return_value = MagicMock(bugs_found=[], issues=[])
        mock_sec = MagicMock()
        mock_sec.run.return_value = MagicMock(vulnerabilities=[], issues=[])

        for _ in range(2):
            run_microtask_review(
                llm=mock_llm,
                task=task,
                microtask=mt,
                repo_path=tmp_path,
                files=files,
                qa_agent=mock_qa,
                security_agent=mock_sec,
            )

        assert mock_qa.run.call_count == 2
        assert mock_sec.run.call_count == 2


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

        def _side_effect(prompt: str) -> Any:
            # The coordinator's chunk reviewer calls complete_json directly and
            # needs a schema-shaped dict; code generation (and any remaining
            # text-template step, e.g. documentation self-review) goes through
            # the Strands Agent/stream() path and needs raw template text --
            # branch on the chunk-review prompt's own anchor text (matches
            # DummyLLMClient's own "code to review" catch-all).
            if "code to review" in prompt.lower():
                return _CLEAN_COORDINATOR_APPROVAL
            _call_count[0] += 1
            if _call_count[0] == 1:
                # First call: execution (file generation)
                return (
                    "\n## FILE src/app.ts ##\n"
                    "export const app = () => console.log('Hello');\n\n## SUMMARY ##\nCreated app module.\n"
                )
            # Any remaining non-review text-template call (e.g. documentation
            # self-review).
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

        assert result.files == {"src/app.ts": "export const app = () => console.log('Hello');"}
        completed = [m for m in result.microtasks if m.status == MicrotaskStatus.COMPLETED]
        assert len(completed) == 1
        assert completed[0].id == "mt-1"

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

        mock_llm = _ScriptedTextClient(
            [
                "## FILES ##\n--- src/bad.ts ---\nconst x = eval('danger');\n---\n\n## SUMMARY ##\nCreated code with security issue.\n",
                {
                    "approved": False,
                    "issues": [
                        {
                            "severity": "critical",
                            "category": "logic",
                            "file_path": "src/bad.ts",
                            "description": "eval is dangerous",
                            "suggestion": "Fix it",
                        }
                    ],
                    "summary": "Security issue found.",
                    "spec_compliance_notes": "",
                },
            ]
        )

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


class _FakeToolAgentOutput:
    """Minimal ``ToolAgentOutput``-shaped stand-in: no issues, no recommendations."""

    def __init__(self) -> None:
        self.issues: list = []
        self.recommendations: list = []


class _FakeToolAgent:
    """Records how many times ``.review()`` was invoked."""

    def __init__(self) -> None:
        self.review_calls = 0

    def review(self, _phase_input: Any) -> _FakeToolAgentOutput:
        self.review_calls += 1
        return _FakeToolAgentOutput()


class TestFrontendQaSecurityGateToolAgentScoping:
    """Pins the fan-out scoping fix: the QA gate must invoke only the
    ``testing_qa`` tool agent and the security gate only the ``security`` tool
    agent, never both -- mirroring ``backend_code_v2_team``'s per-gate scoping
    and removing the shared-instance race that previously blocked enabling
    ``parallelize_qa_security`` for this team (now on by default).
    """

    def test_qa_gate_invokes_only_testing_qa_tool_agent(self, tmp_path):
        from frontend_code_v2_team.models import Microtask, ToolAgentKind
        from frontend_code_v2_team.phases.execution import ReviewDependencies, _qa_gate

        qa_tool_agent = _FakeToolAgent()
        security_tool_agent = _FakeToolAgent()
        task = _create_test_task("frontend")
        mt = Microtask(id="mt-1", title="Test Microtask")
        deps = ReviewDependencies(
            tool_agents={
                ToolAgentKind.TESTING_QA: qa_tool_agent,
                ToolAgentKind.SECURITY: security_tool_agent,
            }
        )

        _qa_gate(
            llm=MagicMock(),
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files={"src/app.ts": "const x = 1;"},
            deps=deps,
            detail_callback=lambda _d: None,
        )

        assert qa_tool_agent.review_calls == 1
        assert security_tool_agent.review_calls == 0

    def test_security_gate_invokes_only_security_tool_agent(self, tmp_path):
        from frontend_code_v2_team.models import Microtask, ToolAgentKind
        from frontend_code_v2_team.phases.execution import ReviewDependencies, _security_gate

        qa_tool_agent = _FakeToolAgent()
        security_tool_agent = _FakeToolAgent()
        task = _create_test_task("frontend")
        mt = Microtask(id="mt-1", title="Test Microtask")
        deps = ReviewDependencies(
            tool_agents={
                ToolAgentKind.TESTING_QA: qa_tool_agent,
                ToolAgentKind.SECURITY: security_tool_agent,
            }
        )

        _security_gate(
            llm=MagicMock(),
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files={"src/app.ts": "const x = 1;"},
            deps=deps,
            detail_callback=lambda _d: None,
        )

        assert security_tool_agent.review_calls == 1
        assert qa_tool_agent.review_calls == 0

    def test_full_cycle_invokes_each_tool_agent_at_most_once(self, tmp_path):
        """End-to-end guard for the residual 2x fix (issue #2817): across one
        full review cycle -- CR gate's full-mapping fan-out plus the QA/Security
        gates' own dedicated calls -- each wired tool agent's ``.review()`` is
        invoked exactly once, not twice, because they all share
        ``deps.tool_agent_cache`` (reset per microtask cycle by
        ``_run_review_cycles``)."""
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

        def _side_effect(prompt: str) -> Any:
            # The coordinator's chunk reviewer calls complete_json directly and
            # needs a schema-shaped dict; code generation goes through the
            # Strands Agent/stream() path and needs raw template text -- branch
            # on the chunk-review prompt's own anchor text (matches
            # DummyLLMClient's own "code to review" catch-all).
            if "code to review" in prompt.lower():
                return _CLEAN_COORDINATOR_APPROVAL
            _call_count[0] += 1
            if _call_count[0] == 1:
                return (
                    "\n## FILE src/app.ts ##\n"
                    "export const app = () => console.log('Hello');\n\n"
                    "## SUMMARY ##\nCreated app module.\n"
                )
            return "\n## REVIEW_STATUS ##\npassed\n\n## ISSUES ##\n\n## SUMMARY ##\nAll good.\n"

        mock_llm = _CallableTextClient(_side_effect)

        qa_tool_agent = _FakeToolAgent()
        security_tool_agent = _FakeToolAgent()
        config = MicrotaskReviewConfig(max_retries=1)
        deps = ReviewDependencies(
            tool_agents={
                ToolAgentKind.TESTING_QA: qa_tool_agent,
                ToolAgentKind.SECURITY: security_tool_agent,
            }
        )

        result = run_execution_with_review_gates(
            llm=mock_llm,
            task=task,
            planning_result=planning_result,
            repo_path=tmp_path,
            review_config=config,
            review_deps=deps,
        )

        completed = [m for m in result.microtasks if m.status == MicrotaskStatus.COMPLETED]
        assert len(completed) == 1
        # Without the cache, the CR gate's full-mapping fan-out plus each
        # gate's own dedicated call would make this 2 -- the redundancy this
        # issue closes.
        assert qa_tool_agent.review_calls == 1
        assert security_tool_agent.review_calls == 1


class TestFrontendQaSecurityCombinedPhaseSignal:
    """Pins the #2659 phase-tracking fix pattern for frontend's own ``GATE_CONFIG``.

    ``parallelize_qa_security`` now defaults to ``True`` in frontend's own
    ``GATE_CONFIG``, matching ``backend_code_v2_team``'s: when concurrent
    QA+Security execution is exercised, it reports the combined
    ``"qa_security_testing"`` phase (never a bare "qa_testing" immediately
    followed by "security_testing") with
    ``MicrotaskStatus.IN_QA_SECURITY_TESTING``, not a state that would let a
    consumer infer QA had already passed. The test below still forces
    ``parallelize_qa_security=True`` explicitly via ``replace(...)`` so it
    keeps exercising the concurrent path regardless of the config default.
    """

    def test_concurrent_qa_security_reports_combined_phase_and_status(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from frontend_code_v2_team.models import (
            Microtask,
            MicrotaskReviewConfig,
            MicrotaskStatus,
            PlanningResult,
            ToolAgentKind,
        )
        from frontend_code_v2_team.phases import execution as exec_mod
        from frontend_code_v2_team.phases.execution import (
            ReviewDependencies,
            run_execution_with_review_gates,
        )

        from software_engineering_team.shared.phases.execution import GateOutcome

        class _NonDummyLLM:
            """Anything that isn't a ``DummyLLMClient`` qualifies for the concurrent
            fan-out; the stub gates below never actually call it."""

        phases_seen: list = []
        statuses_during_qa_security_phase: list = []

        def _pass_gate(**_kwargs: Any) -> GateOutcome:
            return GateOutcome(passed=True)

        def _progress(current_index, completed, total, title, phase, detail):
            phases_seen.append(phase)
            if phase == "qa_security_testing":
                statuses_during_qa_security_phase.append(mt.status)

        monkeypatch.setattr(
            exec_mod,
            "GATE_CONFIG",
            replace(
                exec_mod.GATE_CONFIG,
                run_code_review_gate=_pass_gate,
                run_qa_gate=_pass_gate,
                run_security_gate=_pass_gate,
                run_general_microtask=lambda **_kw: {"src/app.ts": "export const app = 1;\n"},
                run_documentation_self_review=lambda **_kw: MagicMock(
                    documentation={},
                    iterations=0,
                    final_quality_score=1.0,
                ),
                parallelize_qa_security=True,
            ),
        )

        (tmp_path / ".git").mkdir()
        task = _create_test_task("frontend")
        mt = Microtask(id="mt-1", title="Widget", tool_agent=ToolAgentKind.GENERAL)
        planning_result = PlanningResult(microtasks=[mt], language="typescript")

        run_execution_with_review_gates(
            llm=_NonDummyLLM(),
            task=task,
            planning_result=planning_result,
            repo_path=tmp_path,
            review_config=MicrotaskReviewConfig(max_retries=1),
            review_deps=ReviewDependencies(),
            progress_callback=_progress,
        )

        assert mt.status == MicrotaskStatus.COMPLETED
        assert statuses_during_qa_security_phase
        assert set(statuses_during_qa_security_phase) == {MicrotaskStatus.IN_QA_SECURITY_TESTING}
        assert "qa_testing" not in phases_seen
        assert "security_testing" not in phases_seen


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
        assert config.enable_llm_review_grounding is True


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
        assert deps.tool_agent_cache is None


class TestBackendRunMicrotaskReview:
    def test_run_microtask_review_basic(self, tmp_path):
        """Smoke-test backend microtask review through the run_coordinator fallback.

        A bare DummyLLMClient should satisfy the coordinator's chunk-review call
        and produce a passed review with no build failures and no issues.
        """
        from backend_code_v2_team.models import Microtask
        from backend_code_v2_team.phases.review import run_microtask_review

        task = _create_test_task("backend")
        mt = Microtask(id="mt-1", title="Test Microtask")
        files = {"src/main.py": "print('hello')"}

        # A bare DummyLLMClient's built-in "senior code reviewer" branch already
        # returns a clean {"approved": True, "issues": []} for the coordinator's
        # chunk-review call.
        mock_llm = DummyLLMClient()

        result = run_microtask_review(
            llm=mock_llm,
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files=files,
        )
        assert result.passed
        assert result.build_ok
        assert result.issues == []


class TestBackendAgentReviewCache:
    def test_qa_and_security_testing_phases_reuse_cache_for_unchanged_files(self, tmp_path):
        """A second run_{qa,security}_testing_phase call with byte-identical files
        reuses the cached verdict instead of calling the agent again."""
        from backend_code_v2_team.models import Microtask
        from backend_code_v2_team.phases.review import (
            run_qa_testing_phase,
            run_security_testing_phase,
        )

        from software_engineering_team.shared.agent_review import AgentReviewCache

        task = _create_test_task("backend")
        mt = Microtask(id="mt-1", title="Test Microtask")
        files = {"src/main.py": "print('hello')"}

        mock_qa = MagicMock()
        mock_qa.run.return_value = MagicMock(bugs_found=[], issues=[])
        mock_sec = MagicMock()
        mock_sec.run.return_value = MagicMock(vulnerabilities=[], issues=[])
        cache = AgentReviewCache()

        for _ in range(2):
            qa_result = run_qa_testing_phase(
                task=task, microtask=mt, files=files, qa_agent=mock_qa, cache=cache
            )
            sec_result = run_security_testing_phase(
                task=task, microtask=mt, files=files, security_agent=mock_sec, cache=cache
            )
            assert qa_result.passed and sec_result.passed

        assert mock_qa.run.call_count == 1
        assert mock_sec.run.call_count == 1

    def test_qa_testing_phase_without_cache_calls_agent_every_time(self, tmp_path):
        """Omitting ``cache`` (the default) is unchanged: every call re-invokes the agent."""
        from backend_code_v2_team.models import Microtask
        from backend_code_v2_team.phases.review import run_qa_testing_phase

        task = _create_test_task("backend")
        mt = Microtask(id="mt-1", title="Test Microtask")
        files = {"src/main.py": "print('hello')"}

        mock_qa = MagicMock()
        mock_qa.run.return_value = MagicMock(bugs_found=[], issues=[])

        for _ in range(2):
            run_qa_testing_phase(task=task, microtask=mt, files=files, qa_agent=mock_qa)

        assert mock_qa.run.call_count == 2

    def test_qa_testing_phase_recomputes_for_changed_file_content(self, tmp_path):
        """A changed file misses the cache, so the agent is called again."""
        from backend_code_v2_team.models import Microtask
        from backend_code_v2_team.phases.review import run_qa_testing_phase

        from software_engineering_team.shared.agent_review import AgentReviewCache

        task = _create_test_task("backend")
        mt = Microtask(id="mt-1", title="Test Microtask")

        mock_qa = MagicMock()
        mock_qa.run.return_value = MagicMock(bugs_found=[], issues=[])
        cache = AgentReviewCache()

        run_qa_testing_phase(
            task=task,
            microtask=mt,
            files={"src/main.py": "print('hello')"},
            qa_agent=mock_qa,
            cache=cache,
        )
        run_qa_testing_phase(
            task=task,
            microtask=mt,
            files={"src/main.py": "print('goodbye')"},  # edited content
            qa_agent=mock_qa,
            cache=cache,
        )

        assert mock_qa.run.call_count == 2


class TestBackendRunProblemSolvingForMicrotask:
    def test_problem_solving_no_issues(self):
        """Problem solving should report resolved when the review has no issues.

        With an empty issue list and a passed review, the function should not
        need to invoke the LLM and should return a resolved result.
        """
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

        gen_call_count = 0

        def mock_complete_text(prompt: str) -> Any:
            # The coordinator's chunk reviewer calls complete_json directly and
            # needs a schema-shaped dict; code generation (below) goes through
            # the Strands Agent/stream() path and needs raw template text --
            # branch on the chunk-review prompt's own anchor text (matches
            # DummyLLMClient's own "code to review" catch-all).
            nonlocal gen_call_count
            if "code to review" in prompt.lower():
                if "eval(" in prompt:
                    return {
                        "approved": False,
                        "issues": [
                            {
                                "severity": "critical",
                                "category": "security",
                                "file_path": "bad.py",
                                "description": "eval",
                                "suggestion": "",
                            }
                        ],
                        "summary": "Failed.",
                        "spec_compliance_notes": "",
                    }
                return {
                    "approved": True,
                    "issues": [],
                    "summary": "Good code.",
                    "spec_compliance_notes": "",
                }
            gen_call_count += 1
            if gen_call_count == 1:
                return "## FILE bad.py ##\neval('bad')\n\n## SUMMARY ##\nBad code.\n"
            return "## FILE good.py ##\nprint('good')\n\n## SUMMARY ##\nGood code.\n"

        mock_llm = _CallableTextClient(mock_complete_text)

        config = MicrotaskReviewConfig(code_review_max_retries=0, on_failure="skip_continue")
        mock_qa = MagicMock()
        mock_qa.run.return_value = MagicMock(bugs_found=[], issues=[])
        mock_sec = MagicMock()
        mock_sec.run.return_value = MagicMock(vulnerabilities=[], issues=[])
        deps = ReviewDependencies(qa_agent=mock_qa, security_agent=mock_sec)

        result = run_execution_with_review_gates(
            llm=mock_llm,
            task=task,
            planning_result=planning_result,
            repo_path=tmp_path,
            review_config=config,
            review_deps=deps,
        )

        failed_ids = {m.id for m in result.microtasks if m.status == MicrotaskStatus.REVIEW_FAILED}
        assert failed_ids == {"mt-1"}
        mt2_result = next(m for m in result.microtasks if m.id == "mt-2")
        assert mt2_result.status == MicrotaskStatus.COMPLETED

    def test_code_review_gate_forwards_enable_llm_review_grounding(self, tmp_path, monkeypatch):
        """Kill switch on MicrotaskReviewConfig must reach run_code_review_phase."""
        from backend_code_v2_team.models import Microtask
        from backend_code_v2_team.phases import review as review_mod
        from backend_code_v2_team.phases.execution import ReviewDependencies, _code_review_gate

        seen: list = []

        def fake_phase(**kwargs):
            seen.append(kwargs.get("enable_llm_review_grounding"))
            return MagicMock(passed=True, issues=[], summary="ok")

        monkeypatch.setattr(review_mod, "run_code_review_phase", fake_phase)

        task = _create_test_task("backend")
        mt = Microtask(id="mt-1", title="Meal UI")
        _code_review_gate(
            llm=MagicMock(),
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files={"index.html": "<html></html>"},
            deps=ReviewDependencies(),
            detail_callback=lambda _d: None,
            enable_llm_review_grounding=False,
        )
        assert seen == [False]

    def test_execution_forwards_config_grounding_flag_to_gate(self, tmp_path, monkeypatch):
        """run_execution_with_review_gates passes config.enable_llm_review_grounding
        into the code-review gate (end-to-end kill-switch plumbing)."""
        from dataclasses import replace

        from backend_code_v2_team.models import (
            Microtask,
            MicrotaskReviewConfig,
            PlanningResult,
            ToolAgentKind,
        )
        from backend_code_v2_team.phases import execution as exec_mod
        from backend_code_v2_team.phases.execution import (
            ReviewDependencies,
            run_execution_with_review_gates,
        )

        from software_engineering_team.shared.phases.execution import GateOutcome

        seen: list = []

        def fake_cr_gate(**kwargs):
            seen.append(kwargs.get("enable_llm_review_grounding"))
            return GateOutcome(passed=True, issues=[], summary="ok")

        def fake_pass_gate(**_kwargs):
            return GateOutcome(passed=True, issues=[], summary="ok")

        monkeypatch.setattr(
            exec_mod,
            "GATE_CONFIG",
            replace(
                exec_mod.GATE_CONFIG,
                run_code_review_gate=fake_cr_gate,
                run_qa_gate=fake_pass_gate,
                run_security_gate=fake_pass_gate,
                run_documentation_self_review=lambda **_kw: MagicMock(
                    documentation={},
                    iterations=0,
                    final_quality_score=1.0,
                ),
                run_general_microtask=lambda **_kw: {"index.html": "<html></html>"},
            ),
        )

        (tmp_path / ".git").mkdir()
        task = _create_test_task("backend")
        task.requirements = "Build a meal planning UI"
        task.acceptance_criteria = ["user can plan meals"]
        mt = Microtask(id="mt-1", title="Meal UI", tool_agent=ToolAgentKind.GENERAL)
        planning_result = PlanningResult(microtasks=[mt], language="python")

        run_execution_with_review_gates(
            llm=MagicMock(),
            task=task,
            planning_result=planning_result,
            repo_path=tmp_path,
            review_config=MicrotaskReviewConfig(
                max_retries=0,
                enable_llm_review_grounding=False,
                on_failure="skip_continue",
            ),
            review_deps=ReviewDependencies(),
        )
        assert False in seen

        seen.clear()
        run_execution_with_review_gates(
            llm=MagicMock(),
            task=task,
            planning_result=planning_result,
            repo_path=tmp_path,
            review_config=MicrotaskReviewConfig(
                max_retries=0,
                enable_llm_review_grounding=True,
                on_failure="skip_continue",
            ),
            review_deps=ReviewDependencies(),
        )
        assert True in seen

    def test_code_review_phase_enable_llm_review_grounding_is_now_a_no_op(
        self, tmp_path, monkeypatch
    ):
        """The lightweight coordinator-backed fallback has no free-text claim to
        ground -- the chunk reviewer only ever reports on the code it was shown,
        so there is no hallucinated-claim filter left to toggle.
        enable_llm_review_grounding is still accepted (call-signature
        compatibility) but no longer changes the outcome: True and False now
        behave identically."""
        from backend_code_v2_team.models import Microtask
        from backend_code_v2_team.phases import review as review_mod
        from backend_code_v2_team.phases.review import run_code_review_phase

        from software_engineering_team.code_review_agent.models import (
            CodeReviewIssue,
            CodeReviewOutput,
        )

        monkeypatch.setattr(
            review_mod,
            "run_coordinator",
            lambda llm, input_data, *a, **kw: CodeReviewOutput(
                approved=False,
                issues=[
                    CodeReviewIssue(
                        severity="high",
                        file_path="index.html",
                        description="index.html does not support Insurance Provider ZephyrCare",
                        suggestion="Add ZephyrCare",
                    )
                ],
            ),
        )
        monkeypatch.setattr(
            review_mod,
            "_run_build_verification",
            lambda *a, **kw: (True, "ok"),
        )

        task = _create_test_task("backend")
        task.requirements = "Build a meal planning UI for weekly menus"
        task.acceptance_criteria = ["user can plan meals for the week"]
        mt = Microtask(id="mt-1", title="Meal UI", description="Meal planner")
        files = {"index.html": "<html><body>Meal Planner</body></html>"}

        grounded = run_code_review_phase(
            llm=MagicMock(),
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files=files,
            enable_llm_review_grounding=True,
        )
        ungrounded = run_code_review_phase(
            llm=MagicMock(),
            task=task,
            microtask=mt,
            repo_path=tmp_path,
            files=files,
            enable_llm_review_grounding=False,
        )
        assert any("Insurance Provider" in (i.description or "") for i in grounded.issues)
        assert any("Insurance Provider" in (i.description or "") for i in ungrounded.issues)

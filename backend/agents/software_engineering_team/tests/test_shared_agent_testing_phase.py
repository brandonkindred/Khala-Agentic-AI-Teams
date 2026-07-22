"""Unit tests for shared QA/security testing-phase helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from software_engineering_team.shared.models import Task, TaskType
from software_engineering_team.shared.v2_models import ReviewIssue


def _task() -> Task:
    return Task(id="t-1", type=TaskType.QA, title="T", description="desc", assignee="qa")


def _microtask() -> Any:
    return SimpleNamespace(id="mt-1", title="MT", description="do thing")


class _PhaseResult:
    def __init__(self, *, passed, issues, summary, phase_name, **_kwargs):
        self.passed = passed
        self.issues = issues
        self.summary = summary
        self.phase_name = phase_name


class _PhaseInput:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_run_qa_testing_phase_impl_contains_agent_failure():
    from software_engineering_team.shared.phases.review import run_qa_testing_phase_impl

    def _boom(**_kw):
        raise RuntimeError("qa agent exploded")

    result = run_qa_testing_phase_impl(
        task=_task(),
        microtask=_microtask(),
        files={"x.py": "code"},
        review_agent=object(),
        agent_runner=_boom,
        phase_review_result_cls=_PhaseResult,
        tool_phase_input_factory=_PhaseInput,
    )

    assert result.passed is False
    assert result.phase_name == "qa"
    assert any(
        i.source == "qa" and i.severity == "high" and "qa agent exploded" in i.description
        for i in result.issues
    )


def test_run_security_testing_phase_impl_contains_agent_failure():
    from software_engineering_team.shared.phases.review import run_security_testing_phase_impl

    def _boom(**_kw):
        raise RuntimeError("security agent exploded")

    result = run_security_testing_phase_impl(
        task=_task(),
        microtask=_microtask(),
        files={"x.py": "code"},
        review_agent=object(),
        agent_runner=_boom,
        phase_review_result_cls=_PhaseResult,
        tool_phase_input_factory=_PhaseInput,
    )

    assert result.passed is False
    assert result.phase_name == "security"
    assert any(
        i.source == "security"
        and i.severity == "critical"
        and "security agent exploded" in i.description
        for i in result.issues
    )


def test_run_agent_testing_phase_skips_gate_when_no_agents():
    from software_engineering_team.shared.phases.review import (
        _QA_TESTING_PHASE_SPEC,
        _run_agent_testing_phase,
    )

    def _unused(**_kw) -> List[ReviewIssue]:
        raise AssertionError("agent_runner must not run when review_agent is None")

    result = _run_agent_testing_phase(
        spec=_QA_TESTING_PHASE_SPEC,
        task=_task(),
        microtask=_microtask(),
        files={"x.py": "code"},
        review_agent=None,
        agent_runner=_unused,
        tool_agents=None,
        repo_path=None,
        detail_callback=None,
        language="python",
        phase_review_result_cls=_PhaseResult,
        tool_phase_input_factory=_PhaseInput,
    )

    assert result.passed is False
    assert any(
        i.source == "qa" and i.severity == "high" and "QA agent not available" in i.description
        for i in result.issues
    )


def test_run_agent_testing_phase_invokes_tool_agent_via_factory():
    from software_engineering_team.shared.phases.review import (
        _QA_TESTING_PHASE_SPEC,
        _run_agent_testing_phase,
    )

    captured: Dict[str, Any] = {}

    class _Tool:
        def review(self, phase_inp):
            captured["phase_inp"] = phase_inp
            return SimpleNamespace(
                issues=[
                    ReviewIssue(
                        source="qa",
                        severity="medium",
                        description="tool finding",
                        recommendation="",
                    )
                ]
            )

    def _agent_runner(**_kw) -> List[ReviewIssue]:
        return []

    result = _run_agent_testing_phase(
        spec=_QA_TESTING_PHASE_SPEC,
        task=_task(),
        microtask=_microtask(),
        files={"x.py": "code"},
        review_agent=object(),
        agent_runner=_agent_runner,
        tool_agents={_QA_TESTING_PHASE_SPEC.tool_kind: _Tool()},
        repo_path=None,
        detail_callback=None,
        language="python",
        phase_review_result_cls=_PhaseResult,
        tool_phase_input_factory=_PhaseInput,
    )

    assert result.passed is True
    assert any(i.description == "tool finding" for i in result.issues)
    assert captured["phase_inp"].kwargs["phase"].value == "review"
    assert captured["phase_inp"].kwargs["current_files"] == {"x.py": "code"}


def test_run_security_testing_phase_skips_gate_when_no_agents():
    from software_engineering_team.shared.phases.review import (
        _SECURITY_TESTING_PHASE_SPEC,
        _run_agent_testing_phase,
    )

    def _unused(**_kw) -> List[ReviewIssue]:
        raise AssertionError("agent_runner must not run when review_agent is None")

    result = _run_agent_testing_phase(
        spec=_SECURITY_TESTING_PHASE_SPEC,
        task=_task(),
        microtask=_microtask(),
        files={"x.py": "code"},
        review_agent=None,
        agent_runner=_unused,
        tool_agents=None,
        repo_path=None,
        detail_callback=None,
        language="python",
        phase_review_result_cls=_PhaseResult,
        tool_phase_input_factory=_PhaseInput,
    )

    assert result.passed is False
    assert result.phase_name == "security"
    assert any(
        i.source == "security"
        and i.severity == "critical"
        and "Security agent not available" in i.description
        for i in result.issues
    )


def test_run_agent_testing_phase_tool_agent_exception_is_contained():
    from software_engineering_team.shared.phases.review import (
        _QA_TESTING_PHASE_SPEC,
        _run_agent_testing_phase,
    )

    class _ExplodingTool:
        def review(self, _phase_inp):
            raise RuntimeError("tool agent exploded")

    def _agent_runner(**_kw) -> List[ReviewIssue]:
        return []

    result = _run_agent_testing_phase(
        spec=_QA_TESTING_PHASE_SPEC,
        task=_task(),
        microtask=_microtask(),
        files={"x.py": "code"},
        review_agent=object(),
        agent_runner=_agent_runner,
        tool_agents={_QA_TESTING_PHASE_SPEC.tool_kind: _ExplodingTool()},
        repo_path=None,
        detail_callback=None,
        language="python",
        phase_review_result_cls=_PhaseResult,
        tool_phase_input_factory=_PhaseInput,
    )

    assert result.passed is True
    assert result.issues == []


def test_run_agent_testing_phase_invokes_detail_callback():
    from software_engineering_team.shared.phases.review import (
        _QA_TESTING_PHASE_SPEC,
        _run_agent_testing_phase,
    )

    messages: List[str] = []

    class _Tool:
        def review(self, _phase_inp):
            return SimpleNamespace(issues=[])

    def _agent_runner(**_kw) -> List[ReviewIssue]:
        return []

    _run_agent_testing_phase(
        spec=_QA_TESTING_PHASE_SPEC,
        task=_task(),
        microtask=_microtask(),
        files={"x.py": "code"},
        review_agent=object(),
        agent_runner=_agent_runner,
        tool_agents={_QA_TESTING_PHASE_SPEC.tool_kind: _Tool()},
        repo_path=None,
        detail_callback=messages.append,
        language="python",
        phase_review_result_cls=_PhaseResult,
        tool_phase_input_factory=_PhaseInput,
    )

    assert _QA_TESTING_PHASE_SPEC.detail_run_msg in messages
    assert _QA_TESTING_PHASE_SPEC.tool_detail_msg in messages


def test_run_code_review_phase_impl_build_failure_adds_critical_issue():
    from pathlib import Path

    from software_engineering_team.backend_code_v2_team.phases._profile import REVIEW_CONFIG
    from software_engineering_team.shared.phases.review import run_code_review_phase_impl

    class _CrOut:
        issues: List[ReviewIssue] = []
        raw_issue_count = 0

    result = run_code_review_phase_impl(
        llm=object(),
        task=_task(),
        microtask=_microtask(),
        repo_path=Path("/tmp/repo"),
        files={"x.py": "code"},
        build_verify_fn=lambda *_: (False, "compile error"),
        llm_review_fn=lambda **_kw: _CrOut(),
        phase_review_result_cls=_PhaseResult,
        config=REVIEW_CONFIG,
    )

    assert result.passed is False
    assert result.phase_name == "code_review"
    assert any(
        i.source == "build" and i.severity == "critical" and "compile error" in i.description
        for i in result.issues
    )


def test_run_code_review_phase_impl_maps_lint_findings():
    from pathlib import Path

    from software_engineering_team.backend_code_v2_team.phases._profile import REVIEW_CONFIG
    from software_engineering_team.shared.phases.review import run_code_review_phase_impl

    class _LintIssue:
        file_path = "x.py"
        severity = "error"
        message = "unused import"

    class _LintResult:
        linter_issues = [_LintIssue()]
        execution_result = SimpleNamespace(success=False)

    class _LintAgent:
        def run(self, _inp):
            return _LintResult()

    class _CrOut:
        issues: List[ReviewIssue] = []
        raw_issue_count = 0

    messages: List[str] = []

    result = run_code_review_phase_impl(
        llm=object(),
        task=_task(),
        microtask=_microtask(),
        repo_path=Path("/tmp/repo"),
        files={"x.py": "code"},
        linting_tool_agent=_LintAgent(),
        detail_callback=messages.append,
        build_verify_fn=lambda *_: (True, "ok"),
        llm_review_fn=lambda **_kw: _CrOut(),
        phase_review_result_cls=_PhaseResult,
        config=REVIEW_CONFIG,
    )

    assert "Running build verification..." in messages
    assert "Running linter..." in messages
    assert any(i.source == "lint" and i.description == "unused import" for i in result.issues)
    assert result.passed is False

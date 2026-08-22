"""Unit tests for shared QA/security testing-phase helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from shared.dev_models.models import Task, TaskType
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
    """QA agent_runner failure becomes a synthetic high issue; phase fails without raising."""
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
        tool_phase_includes_context=True,
    )

    assert result.passed is False
    assert result.phase_name == "qa"
    assert any(
        i.source == "qa" and i.severity == "high" and "qa agent exploded" in i.description
        for i in result.issues
    )


def test_run_security_testing_phase_impl_contains_agent_failure():
    """Security agent_runner failure becomes a synthetic critical issue; phase fails without raising."""
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
        tool_phase_includes_context=True,
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
    """With neither QA agent nor tool agent, the gate synthesizes a skip issue and fails."""
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
        tool_phase_includes_context=True,
    )

    assert result.passed is False
    assert any(
        i.source == "qa" and i.severity == "high" and "QA agent not available" in i.description
        for i in result.issues
    )


def test_run_agent_testing_phase_invokes_tool_agent_via_factory():
    """Wired tool agent receives a factory-built phase input and its issues are folded in."""
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
        tool_phase_includes_context=True,
    )

    assert result.passed is True
    assert any(i.description == "tool finding" for i in result.issues)
    assert captured["phase_inp"].kwargs["phase"].value == "review"
    assert captured["phase_inp"].kwargs["current_files"] == {"x.py": "code"}
    assert captured["phase_inp"].kwargs["existing_code"] == ""
    assert captured["phase_inp"].kwargs["spec_context"] == "desc"
    assert captured["phase_inp"].kwargs["language"] == "python"


def test_run_agent_testing_phase_omits_context_when_flag_false():
    """Mirrors ``_run_tool_agents_review``: frontend's flag omits context fields."""
    from software_engineering_team.shared.phases.review import (
        _QA_TESTING_PHASE_SPEC,
        _run_agent_testing_phase,
    )

    captured: Dict[str, Any] = {}

    class _Tool:
        def review(self, phase_inp):
            captured["phase_inp"] = phase_inp
            return SimpleNamespace(issues=[])

    def _agent_runner(**_kw) -> List[ReviewIssue]:
        return []

    _run_agent_testing_phase(
        spec=_QA_TESTING_PHASE_SPEC,
        task=_task(),
        microtask=_microtask(),
        files={"x.ts": "code"},
        review_agent=object(),
        agent_runner=_agent_runner,
        tool_agents={_QA_TESTING_PHASE_SPEC.tool_kind: _Tool()},
        repo_path=None,
        detail_callback=None,
        language="typescript",
        phase_review_result_cls=_PhaseResult,
        tool_phase_input_factory=_PhaseInput,
        tool_phase_includes_context=False,
    )

    kwargs = captured["phase_inp"].kwargs
    assert "existing_code" not in kwargs
    assert "spec_context" not in kwargs
    assert "language" not in kwargs
    assert kwargs["current_files"] == {"x.ts": "code"}


def test_run_security_testing_phase_skips_gate_when_no_agents():
    """With neither security agent nor tool agent, the gate synthesizes a skip issue and fails."""
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
        tool_phase_includes_context=True,
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
    """A tool-agent .review() exception is logged and swallowed; phase may still pass."""
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
        tool_phase_includes_context=True,
    )

    assert result.passed is True
    assert result.issues == []


def test_run_agent_testing_phase_invokes_detail_callback():
    """Detail callback receives both the agent-run and tool-agent review progress messages."""
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
        tool_phase_includes_context=True,
    )

    assert _QA_TESTING_PHASE_SPEC.detail_run_msg in messages
    assert _QA_TESTING_PHASE_SPEC.tool_detail_msg in messages


def test_run_code_review_phase_impl_runs_code_review_step_standalone():
    """The phase runs only the code-review step: no build/lint progress messages, and a
    clean code-review result passes with no issues — build/lint no longer gate this phase."""
    from pathlib import Path

    from software_engineering_team.shared.phases.review import run_code_review_phase_impl
    from software_engineering_team.shared.v2_review import LlmReviewOutput

    messages: List[str] = []

    result = run_code_review_phase_impl(
        llm=object(),
        task=_task(),
        microtask=_microtask(),
        repo_path=Path("/tmp/repo"),
        files={"x.py": "code"},
        detail_callback=messages.append,
        llm_review_fn=lambda **_kw: LlmReviewOutput(issues=[], raw_issue_count=0),
        phase_review_result_cls=_PhaseResult,
    )

    assert "Running build verification..." not in messages
    assert "Running linter..." not in messages
    assert "Running code review..." in messages
    assert result.passed is True
    assert result.phase_name == "code_review"
    assert result.issues == []


# ---------------------------------------------------------------------------
# ``_run_qa_agent_impl`` / ``_run_security_agent_impl`` / ``_run_build_verification_impl``
#
# Backing the per-team ``_run_qa_agent`` / ``_run_security_agent`` /
# ``_run_build_verification`` in each code-v2 team's ``phases/_profile.py``
# (Story 3c, Step 2). The per-team review tests already exercise these through
# their team's thin wrapper; these tests drive the shared body directly with a
# synthetic ``issue_factory`` to pin its own contract independent of either
# team's ``ReviewIssue``.
# ---------------------------------------------------------------------------


class _FakeIssue:
    def __init__(self, *, source, severity, description, recommendation, **_kwargs):
        self.source = source
        self.severity = severity
        self.description = description
        self.recommendation = recommendation


def test_run_qa_agent_impl_forwards_issue_factory_and_chunking_knobs():
    """Delegates to the shared ``run_qa_agent``, threading the caller's
    ``issue_factory``/``max_chars``/``warn_threshold`` straight through."""
    from software_engineering_team.shared.phases.review import _run_qa_agent_impl

    captured: Dict[str, Any] = {}

    class _QAAgent:
        def run(self, inp):
            captured["code"] = inp.code
            return SimpleNamespace(bugs_found=[SimpleNamespace(description="bug", severity="high")])

    issues = _run_qa_agent_impl(
        qa_agent=_QAAgent(),
        files={"x.py": "print('hi')"},
        language="python",
        task_description="desc",
        task_id="t1",
        issue_factory=_FakeIssue,
        max_chars=10_000,
        warn_threshold=5,
    )

    assert captured["code"] == "print('hi')"
    assert len(issues) == 1
    assert isinstance(issues[0], _FakeIssue)
    assert issues[0].source == "qa"


def test_run_security_agent_impl_forwards_issue_factory_and_chunking_knobs():
    """Delegates to the shared ``run_security_agent``, threading the caller's
    ``issue_factory``/``max_chars``/``warn_threshold`` straight through."""
    from software_engineering_team.shared.phases.review import _run_security_agent_impl

    captured: Dict[str, Any] = {}

    class _SecAgent:
        def run(self, inp):
            captured["code"] = inp.code
            return SimpleNamespace(
                vulnerabilities=[SimpleNamespace(description="vuln", severity="critical")]
            )

    issues = _run_security_agent_impl(
        security_agent=_SecAgent(),
        files={"x.py": "print('hi')"},
        language="python",
        task_description="desc",
        task_id="t1",
        issue_factory=_FakeIssue,
        max_chars=10_000,
        warn_threshold=5,
    )

    assert captured["code"] == "print('hi')"
    assert len(issues) == 1
    assert isinstance(issues[0], _FakeIssue)
    assert issues[0].source == "security"


def test_run_build_verification_impl_no_verifier_defaults_to_ok():
    """No ``build_verifier`` means the build step is skipped, not failed."""
    from pathlib import Path

    from software_engineering_team.shared.phases.review import _run_build_verification_impl

    ok, msg = _run_build_verification_impl(
        Path("/tmp/repo"), None, "t1", build_verify_label="backend_code_v2"
    )

    assert ok is True
    assert "skipping" in msg.lower()


def test_run_build_verification_impl_forwards_label_to_verifier():
    """The team's ``build_verify_label`` is threaded through to the verifier call."""
    from pathlib import Path

    from software_engineering_team.shared.phases.review import _run_build_verification_impl

    seen: Dict[str, Any] = {}

    def _verifier(repo_path, label, task_id):
        seen["repo_path"] = repo_path
        seen["label"] = label
        seen["task_id"] = task_id
        return True, "build ok"

    ok, msg = _run_build_verification_impl(
        Path("/tmp/repo"), _verifier, "t1", build_verify_label="frontend_code_v2"
    )

    assert ok is True
    assert msg == "build ok"
    assert seen["label"] == "frontend_code_v2"
    assert seen["task_id"] == "t1"


def test_run_build_verification_impl_contains_verifier_exception():
    """An outright verifier exception is contained, not propagated."""
    from pathlib import Path

    from software_engineering_team.shared.phases.review import _run_build_verification_impl

    def _bad(*_a, **_kw):
        raise RuntimeError("build crashed")

    ok, msg = _run_build_verification_impl(
        Path("/tmp/repo"), _bad, "t1", build_verify_label="backend_code_v2"
    )

    assert ok is False
    assert "build crashed" in msg


def test_run_code_review_phase_impl_fails_on_code_review_issue():
    """A critical code-review finding fails the phase — driven solely by
    ``_code_review_step``'s output, with no build/lint step involved."""
    from pathlib import Path

    from software_engineering_team.shared.phases.review import run_code_review_phase_impl
    from software_engineering_team.shared.v2_review import LlmReviewOutput

    cr_issue = ReviewIssue(
        source="code_review",
        severity="critical",
        description="SQL injection risk",
        recommendation="",
    )

    result = run_code_review_phase_impl(
        llm=object(),
        task=_task(),
        microtask=_microtask(),
        repo_path=Path("/tmp/repo"),
        files={"x.py": "code"},
        llm_review_fn=lambda **_kw: LlmReviewOutput(issues=[cr_issue], raw_issue_count=1),
        phase_review_result_cls=_PhaseResult,
    )

    assert result.passed is False
    assert result.phase_name == "code_review"
    assert any(
        i.source == "code_review" and i.severity == "critical" and "SQL injection" in i.description
        for i in result.issues
    )

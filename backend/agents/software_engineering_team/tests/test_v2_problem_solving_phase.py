"""Tests for backend_code_v2_team.phases.problem_solving and helpers."""

from __future__ import annotations

from unittest.mock import MagicMock


def _task(**overrides):
    from software_engineering_team.shared.models import Task, TaskType

    base = dict(
        id="t1",
        type=TaskType.BACKEND,
        title="T",
        description="desc",
        requirements="reqs",
        assignee="backend",
    )
    base.update(overrides)
    return Task(**base)


def _microtask(**overrides):
    from software_engineering_team.backend_code_v2_team.models import (
        Microtask,
        ToolAgentKind,
    )

    base = dict(id="mt-1", title="t", description="d", tool_agent=ToolAgentKind.GENERAL)
    base.update(overrides)
    return Microtask(**base)


def _issue(**overrides):
    from software_engineering_team.backend_code_v2_team.models import ReviewIssue

    base = dict(
        source="code_review",
        severity="high",
        description="bad code",
        file_path="x.py",
        recommendation="fix it",
    )
    base.update(overrides)
    return ReviewIssue(**base)


def _review_result(issues=None):
    from software_engineering_team.backend_code_v2_team.models import ReviewResult

    return ReviewResult(passed=False, issues=issues or [], build_ok=True, lint_ok=True)


class _StubAgent:
    def __init__(self, response, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc

    def __call__(self, prompt):
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def test_format_all_code_truncates():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        _format_all_code,
    )

    huge = {f"f{i}.py": "x" * 1000 for i in range(100)}
    out = _format_all_code(huge, max_chars=2000)
    assert "truncated" in out


def test_format_all_code_empty():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        _format_all_code,
    )

    assert _format_all_code({}) == "(no code)"


def test_format_issues_for_batch():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        _format_issues_for_batch,
    )

    issues = [_issue(), _issue(source="qa", severity="low")]
    out = _format_issues_for_batch(issues)
    assert "Issue 1" in out
    assert "Issue 2" in out
    assert "code_review" in out


def test_relevant_code_for_issue_with_match():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        _relevant_code_for_issue,
    )

    out = _relevant_code_for_issue(_issue(file_path="a.py"), {"a.py": "code"})
    assert "a.py" in out


def test_relevant_code_for_issue_fallback():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        _relevant_code_for_issue,
    )

    out = _relevant_code_for_issue(_issue(file_path="missing.py"), {"a.py": "X", "b.py": "Y"})
    assert "a.py" in out
    assert "b.py" in out


def test_relevant_code_for_issue_empty():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        _relevant_code_for_issue,
    )

    out = _relevant_code_for_issue(_issue(file_path=""), {})
    assert out == "(no code)"


def test_run_batch_coding_fixes_no_actionable_issues():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_batch_coding_fixes,
    )

    out = run_batch_coding_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        issues=[_issue(severity="info")],
        current_files={"a.py": "code"},
    )
    assert out.resolved is True
    assert "No actionable" in out.summary


def test_run_batch_coding_fixes_llm_failure(monkeypatch):
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_batch_coding_fixes,
    )

    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent("", raise_exc=RuntimeError("boom")))
    monkeypatch.setattr(ps_mod, "_resolve_model", lambda llm: object())

    out = run_batch_coding_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        issues=[_issue()],
        current_files={"a.py": "code"},
    )
    assert out.resolved is False
    assert "Batch fix failed" in out.summary


def test_run_batch_coding_fixes_success(monkeypatch):
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_batch_coding_fixes,
    )

    resp = (
        "## FILE a.py ##\nfixed code\n"
        "## ISSUES_ADDRESSED ##\n"
        "issue_index: 1\ndescription: fixed\n"
        "## END ISSUES_ADDRESSED ##\n"
        "## SUMMARY ##\nall fixed\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "_resolve_model", lambda llm: object())

    out = run_batch_coding_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        issues=[_issue()],
        current_files={"a.py": "code"},
    )
    assert "a.py" in out.files
    assert out.files["a.py"] == "fixed code"


def test_run_batch_coding_fixes_partial_with_unresolved(monkeypatch):
    """Some issues addressed, others not -> unresolved_issues populated."""
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_batch_coding_fixes,
    )

    resp = (
        "## FILE a.py ##\nfixed\n"
        "## ISSUES_ADDRESSED ##\n"
        "issue_index: 1\ndescription: fixed first\n"
        "## END ISSUES_ADDRESSED ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "_resolve_model", lambda llm: object())

    out = run_batch_coding_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        issues=[_issue(), _issue(description="second")],
        current_files={"a.py": "code"},
    )
    # Only first issue addressed -> second is unresolved
    assert len(out.unresolved_issues) == 1


def test_run_batch_coding_fixes_with_callback(monkeypatch):
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_batch_coding_fixes,
    )

    monkeypatch.setattr(
        ps_mod,
        "Agent",
        lambda *a, **kw: _StubAgent("## SUMMARY ##\nok\n## END SUMMARY ##\n"),
    )
    monkeypatch.setattr(ps_mod, "_resolve_model", lambda llm: object())

    msgs = []
    run_batch_coding_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        issues=[_issue()],
        current_files={"a.py": "code"},
        detail_callback=msgs.append,
        language="java",
    )
    assert msgs  # callback was invoked


def test_run_problem_solving_no_actionable():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_problem_solving,
    )

    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue(severity="info")]),
        current_files={"a.py": "code"},
    )
    assert out.resolved is True


def test_run_problem_solving_llm_failure(monkeypatch):
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_problem_solving,
    )

    monkeypatch.setattr(
        ps_mod, "Agent", lambda *a, **kw: _StubAgent("", raise_exc=RuntimeError("err"))
    )
    monkeypatch.setattr(ps_mod, "_resolve_model", lambda llm: object())

    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue()]),
        current_files={"a.py": "code"},
    )
    # All issues unresolved
    assert out.resolved is False
    assert len(out.unresolved_issues) == 1


def test_run_problem_solving_fix_success(monkeypatch):
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_problem_solving,
    )

    resp = (
        "## FILE a.py ##\nfixed\n"
        "## RESOLVED ##\nyes\n## END RESOLVED ##\n"
        "## SUMMARY ##\nfixed\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "_resolve_model", lambda llm: object())

    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue()]),
        current_files={"a.py": "old"},
    )
    assert out.resolved is True


def test_run_problem_solving_with_tool_agents(monkeypatch):
    from software_engineering_team.backend_code_v2_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_problem_solving,
    )

    resp = (
        "## FILE a.py ##\nfixed\n"
        "## RESOLVED ##\nyes\n## END RESOLVED ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "_resolve_model", lambda llm: object())

    tool_agent = MagicMock()
    tool_agent.problem_solve.return_value = ToolAgentPhaseOutput(
        files={"b.py": "tool fix"},
        recommendations=["consider X"],
        summary="tool ran",
    )

    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue()]),
        current_files={"a.py": "old"},
        tool_agents={ToolAgentKind.SECURITY: tool_agent},
    )
    assert "b.py" in out.files


def test_run_problem_solving_tool_agent_raises(monkeypatch):
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_problem_solving,
    )

    resp = "## RESOLVED ##\nyes\n## END RESOLVED ##\n## SUMMARY ##\nok\n## END SUMMARY ##\n"
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "_resolve_model", lambda llm: object())

    tool_agent = MagicMock()
    tool_agent.problem_solve.side_effect = RuntimeError("crash")

    # Should not raise
    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue()]),
        current_files={"a.py": "old"},
        tool_agents={ToolAgentKind.SECURITY: tool_agent},
    )
    assert out is not None


def test_run_problem_solving_for_microtask_no_actionable():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_problem_solving_for_microtask,
    )

    out = run_problem_solving_for_microtask(
        llm=MagicMock(),
        microtask=_microtask(),
        review_result=_review_result([_issue(severity="info")]),
        current_files={"a.py": "code"},
    )
    assert out.resolved is True


def test_run_problem_solving_for_microtask_success(monkeypatch):
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_problem_solving_for_microtask,
    )

    resp = (
        "## FILE a.py ##\nfixed\n"
        "## RESOLVED ##\nyes\n## END RESOLVED ##\n"
        "## SUMMARY ##\nfixed\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "_resolve_model", lambda llm: object())

    msgs = []
    out = run_problem_solving_for_microtask(
        llm=MagicMock(),
        microtask=_microtask(),
        review_result=_review_result([_issue()]),
        current_files={"a.py": "old"},
        detail_callback=msgs.append,
    )
    assert out is not None


def _phase_result(issues, phase_name="code_review"):
    from software_engineering_team.backend_code_v2_team.models import PhaseReviewResult

    return PhaseReviewResult(passed=False, issues=issues, phase_name=phase_name)


def test_run_phase_fixes_via_code_review():
    """run_code_review_fixes routes through internal _run_phase_fixes."""
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_code_review_fixes,
    )

    out = run_code_review_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        phase_result=_phase_result([_issue(severity="info", source="code_review")]),
        current_files={"a.py": "code"},
    )
    assert out.resolved is True


def test_run_qa_fixes():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_qa_fixes,
    )

    out = run_qa_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        phase_result=_phase_result([_issue(severity="info", source="qa")], phase_name="qa"),
        current_files={"a.py": "code"},
    )
    assert out.resolved is True


def test_run_security_fixes():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_security_fixes,
    )

    out = run_security_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        phase_result=_phase_result(
            [_issue(severity="info", source="security")], phase_name="security"
        ),
        current_files={"a.py": "code"},
    )
    assert out.resolved is True


def test_run_documentation_fixes():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_documentation_fixes,
    )

    out = run_documentation_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        phase_result=_phase_result(
            [_issue(severity="info", source="documentation")], phase_name="documentation"
        ),
        current_files={"a.py": "code"},
    )
    assert out.resolved is True


def test_run_code_review_fixes_with_actionable(monkeypatch):
    """Exercises _run_phase_fixes via run_code_review_fixes with an actionable issue."""
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_code_review_fixes,
    )

    resp = (
        "## FILE a.py ##\nfixed\n"
        "## RESOLVED ##\nyes\n## END RESOLVED ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "_resolve_model", lambda llm: object())

    out = run_code_review_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        phase_result=_phase_result([_issue(severity="high")]),
        current_files={"a.py": "old"},
    )
    assert isinstance(out.summary, str)

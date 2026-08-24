"""Tests for frontend_code_v2_team.phases.problem_solving and helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _task(**overrides):
    from shared.dev_models.models import Task, TaskType

    base = dict(
        id="t1",
        type=TaskType.FRONTEND,
        title="T",
        description="desc",
        requirements="reqs",
        assignee="frontend",
    )
    base.update(overrides)
    return Task(**base)


def _microtask(**overrides):
    from software_engineering_team.codegen_team.models import (
        Microtask,
        ToolAgentKind,
    )

    base = dict(id="mt-1", title="t", description="d", tool_agent=ToolAgentKind.GENERAL)
    base.update(overrides)
    return Microtask(**base)


def _issue(**overrides):
    from software_engineering_team.codegen_team.models import ReviewIssue

    base = dict(
        source="code_review",
        severity="high",
        description="bad code",
        file_path="x.ts",
        recommendation="fix it",
    )
    base.update(overrides)
    return ReviewIssue(**base)


def _review_result(issues=None):
    from software_engineering_team.codegen_team.models import ReviewResult

    return ReviewResult(passed=False, issues=issues or [], build_ok=True, lint_ok=True)


class _StubAgent:
    def __init__(self, response, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc

    def __call__(self, prompt):
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def test_fe_format_all_code():
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        _format_all_code,
    )

    assert _format_all_code({}) == "(no code)"
    out = _format_all_code({"a.ts": "code"})
    assert "a.ts" in out


def test_fe_format_all_code_truncates():
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        _format_all_code,
    )

    huge = {f"f{i}.ts": "x" * 1000 for i in range(50)}
    out = _format_all_code(huge, max_chars=2000)
    assert "truncated" in out


def test_fe_format_all_code_raises_on_nonpositive_max_chars():
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        _format_all_code,
    )

    with pytest.raises(ValueError):
        _format_all_code({"a.ts": "code"}, max_chars=0)


def test_fe_format_issues_for_batch():
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        _format_issues_for_batch,
    )

    out = _format_issues_for_batch([_issue(), _issue(severity="low")])
    assert "Issue 1" in out
    assert "Issue 2" in out


def test_fe_relevant_code_for_issue():
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        _relevant_code_for_issue,
    )

    out = _relevant_code_for_issue(_issue(file_path="a.ts"), {"a.ts": "code"})
    assert "a.ts" in out


def test_fe_relevant_code_for_issue_fallback():
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        _relevant_code_for_issue,
    )

    out = _relevant_code_for_issue(_issue(file_path="missing.ts"), {"a.ts": "X"})
    assert "a.ts" in out


def test_fe_run_batch_no_actionable():
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        run_batch_coding_fixes,
    )

    out = run_batch_coding_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        issues=[_issue(severity="info")],
        current_files={"a.ts": "code"},
    )
    assert out.resolved is True


def test_fe_run_batch_success(monkeypatch):
    from software_engineering_team.codegen_team.stacks.frontend import problem_solving as ps_mod
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        run_batch_coding_fixes,
    )

    resp = (
        "## FILE a.ts ##\nfixed\n"
        "## ISSUES_ADDRESSED ##\n"
        "issue_index: 1\ndescription: fixed\n"
        "## END ISSUES_ADDRESSED ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = run_batch_coding_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        issues=[_issue()],
        current_files={"a.ts": "code"},
    )
    assert "a.ts" in out.files


def test_fe_run_batch_llm_failure(monkeypatch):
    from software_engineering_team.codegen_team.stacks.frontend import problem_solving as ps_mod
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        run_batch_coding_fixes,
    )

    monkeypatch.setattr(
        ps_mod, "Agent", lambda *a, **kw: _StubAgent("", raise_exc=RuntimeError("boom"))
    )
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = run_batch_coding_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        issues=[_issue()],
        current_files={"a.ts": "code"},
    )
    assert out.resolved is False


def test_fe_run_problem_solving_no_actionable():
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        run_problem_solving,
    )

    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue(severity="info")]),
        current_files={"a.ts": "code"},
    )
    assert out.resolved is True


def test_fe_run_problem_solving_llm_failure(monkeypatch):
    from software_engineering_team.codegen_team.stacks.frontend import problem_solving as ps_mod
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        run_problem_solving,
    )

    monkeypatch.setattr(
        ps_mod, "Agent", lambda *a, **kw: _StubAgent("", raise_exc=RuntimeError("err"))
    )
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue()]),
        current_files={"a.ts": "code"},
    )
    assert out.resolved is False


def test_fe_run_problem_solving_with_tool_agents(monkeypatch):
    from software_engineering_team.codegen_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.codegen_team.stacks.frontend import problem_solving as ps_mod
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        run_problem_solving,
    )

    resp = (
        "## FILE a.ts ##\nfixed\n"
        "## RESOLVED ##\nyes\n## END RESOLVED ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    tool_agent = MagicMock()
    tool_agent.problem_solve.return_value = ToolAgentPhaseOutput(
        files={"b.ts": "tool fix"},
        recommendations=["consider X"],
        summary="tool ran",
    )

    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue()]),
        current_files={"a.ts": "old"},
        tool_agents={ToolAgentKind.DOCUMENTATION: tool_agent},
    )
    assert "b.ts" in out.files


def _fe_review_only_tool_agents_do_not_consult_problem_solve(monkeypatch, kind, agent_cls):
    """Shared body: a review-only frontend tool agent wired into ``tool_agents``
    is never asked for a fix, because the real class has no ``problem_solve`` —
    ``spec=agent_cls`` makes the mock reject that attribute exactly like the
    real class would, unlike a bare ``MagicMock()`` (which fakes every attribute
    and would pass this assertion regardless of the real class's contract)."""
    from software_engineering_team.codegen_team.stacks.frontend import problem_solving as ps_mod
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        run_problem_solving,
    )

    assert not hasattr(agent_cls, "problem_solve")

    resp = (
        "## FILE a.ts ##\nfixed\n"
        "## RESOLVED ##\nyes\n## END RESOLVED ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    tool_agent = MagicMock(spec=agent_cls)

    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue(source=kind.value)]),
        current_files={"a.ts": "old"},
        tool_agents={kind: tool_agent},
    )
    assert not hasattr(tool_agent, "problem_solve")
    assert out.files["a.ts"] == "fixed"


def test_fe_run_problem_solving_accessibility_tool_agent_has_no_problem_solve(monkeypatch):
    from software_engineering_team.codegen_team.models import ToolAgentKind
    from software_engineering_team.codegen_team.tool_agents.frontend.accessibility.agent import (
        AccessibilityToolAgent,
    )

    _fe_review_only_tool_agents_do_not_consult_problem_solve(
        monkeypatch, ToolAgentKind.ACCESSIBILITY, AccessibilityToolAgent
    )


def test_fe_run_problem_solving_performance_tool_agent_has_no_problem_solve(monkeypatch):
    from software_engineering_team.codegen_team.models import ToolAgentKind
    from software_engineering_team.codegen_team.tool_agents.frontend.performance.agent import (
        PerformanceToolAgent,
    )

    _fe_review_only_tool_agents_do_not_consult_problem_solve(
        monkeypatch, ToolAgentKind.PERFORMANCE, PerformanceToolAgent
    )


def test_fe_run_problem_solving_ux_usability_tool_agent_has_no_problem_solve(monkeypatch):
    from software_engineering_team.codegen_team.models import ToolAgentKind
    from software_engineering_team.codegen_team.tool_agents.frontend.ux_usability.agent import (
        UxUsabilityToolAgent,
    )

    _fe_review_only_tool_agents_do_not_consult_problem_solve(
        monkeypatch, ToolAgentKind.UX_USABILITY, UxUsabilityToolAgent
    )


def test_fe_run_problem_solving_for_microtask(monkeypatch):
    from software_engineering_team.codegen_team.stacks.frontend import problem_solving as ps_mod
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        run_problem_solving_for_microtask,
    )

    resp = (
        "## FILE a.ts ##\nfixed\n"
        "## RESOLVED ##\nyes\n## END RESOLVED ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = run_problem_solving_for_microtask(
        llm=MagicMock(),
        microtask=_microtask(),
        review_result=_review_result([_issue()]),
        current_files={"a.ts": "old"},
    )
    assert out is not None


def test_fe_run_problem_solving_for_microtask_no_actionable():
    from software_engineering_team.codegen_team.stacks.frontend.problem_solving import (
        run_problem_solving_for_microtask,
    )

    out = run_problem_solving_for_microtask(
        llm=MagicMock(),
        microtask=_microtask(),
        review_result=_review_result([_issue(severity="info")]),
        current_files={"a.ts": "code"},
    )
    assert out.resolved is True

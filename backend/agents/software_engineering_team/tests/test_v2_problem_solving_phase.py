"""Tests for backend_code_v2_team.phases.problem_solving and helpers."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest


def _task(**overrides):
    from shared.dev_models.models import Task, TaskType

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


def test_format_all_code_raises_on_nonpositive_max_chars():
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        _format_all_code,
    )

    with pytest.raises(ValueError):
        _format_all_code({"f.py": "x"}, max_chars=0)


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

    monkeypatch.setattr(
        ps_mod, "Agent", lambda *a, **kw: _StubAgent("", raise_exc=RuntimeError("boom"))
    )
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

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
        "## FILE a.py ##\nfixed = True\n"
        "## ISSUES_ADDRESSED ##\n"
        "issue_index: 1\ndescription: fixed\n"
        "## END ISSUES_ADDRESSED ##\n"
        "## SUMMARY ##\nall fixed\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = run_batch_coding_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        issues=[_issue()],
        current_files={"a.py": "code"},
    )
    assert "a.py" in out.files
    assert out.files["a.py"] == "fixed = True"


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
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = run_batch_coding_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        issues=[_issue(), _issue(description="second")],
        current_files={"a.py": "code"},
    )
    # Only first issue addressed -> second is unresolved
    assert len(out.unresolved_issues) == 1


def test_run_batch_coding_fixes_rejects_unparsable_python(monkeypatch):
    """A batch fix that returns an incomplete .py rewrite must not be merged."""
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_batch_coding_fixes,
    )

    resp = (
        "## FILE a.py ##\ndef foo(:\n    pass\n"  # unparsable: broken def
        "## ISSUES_ADDRESSED ##\n"
        "issue_index: 1\ndescription: fixed\n"
        "## END ISSUES_ADDRESSED ##\n"
        "## SUMMARY ##\nall fixed\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = run_batch_coding_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        issues=[_issue(file_path="a.py")],
        current_files={"a.py": "original code"},
    )
    # Broken rewrite discarded -- prior content kept, issue stays unresolved.
    assert out.files["a.py"] == "original code"
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
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

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
    assert any("issue" in m.lower() for m in msgs)


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
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

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
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue()]),
        current_files={"a.py": "old"},
    )
    assert out.resolved is True


def test_run_problem_solving_rejects_unparsable_python_even_if_resolved(monkeypatch):
    """A mixed response (one valid file + the issue's own file broken) that
    claims resolved=yes must NOT be trusted -- the issue's file was
    discarded, so the issue must stay unresolved and retry."""
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_problem_solving,
    )

    # a.py (the issue's own file) is unparsable; b.py is valid. The LLM
    # claims resolved=yes despite a.py never actually landing.
    resp = (
        "## FILE a.py ##\ndef broken(:\n    pass\n"
        "## FILE b.py ##\nvalid = True\n"
        "## RESOLVED ##\nyes\n## END RESOLVED ##\n"
        "## SUMMARY ##\nfixed\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue(file_path="a.py")]),
        current_files={"a.py": "original code"},
    )
    # a.py's broken rewrite was discarded -- prior content kept.
    assert out.files["a.py"] == "original code"
    # b.py, valid, did land.
    assert out.files["b.py"] == "valid = True"
    # The issue is NOT considered resolved despite the LLM's claim.
    assert out.resolved is False
    assert len(out.unresolved_issues) == 1
    # No fix entry recorded for an attempt that didn't land the issue's file.
    assert out.fixes_applied == []


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
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    tool_agent = MagicMock()
    tool_agent.problem_solve.return_value = ToolAgentPhaseOutput(
        files={"b.py": "tool_fix = True"},
        recommendations=["consider X"],
        summary="tool ran",
    )

    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue()]),
        current_files={"a.py": "old"},
        tool_agents={ToolAgentKind.DOCUMENTATION: tool_agent},
    )
    assert out.files["b.py"] == "tool_fix = True"
    assert out.files["a.py"] == "fixed"
    assert out.resolved is True
    assert out.unresolved_issues == []


def test_run_problem_solving_tool_agent_partial_rejection(monkeypatch):
    """A tool agent returning a mix of valid and unparsable Python files must
    only merge the valid ones; the unparsable file is discarded."""
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
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    tool_agent = MagicMock()
    tool_agent.problem_solve.return_value = ToolAgentPhaseOutput(
        files={"b.py": "valid = True", "c.py": "def broken(:\n    pass\n"},
        recommendations=["consider X"],
        summary="tool ran",
    )

    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue()]),
        current_files={"a.py": "old", "c.py": "prior c content"},
        tool_agents={ToolAgentKind.DOCUMENTATION: tool_agent},
    )
    assert out.files["b.py"] == "valid = True"
    # c.py's broken rewrite was discarded -- prior content kept.
    assert out.files["c.py"] == "prior c content"


def test_run_problem_solving_tool_agent_raises(monkeypatch):
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_problem_solving,
    )

    resp = "## RESOLVED ##\nyes\n## END RESOLVED ##\n## SUMMARY ##\nok\n## END SUMMARY ##\n"
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    tool_agent = MagicMock()
    tool_agent.problem_solve.side_effect = RuntimeError("crash")

    # Should not raise -- the exception is caught inside
    # _apply_tool_agents_problem_solve and logged, so the tool agent's
    # (nonexistent) file updates never reach the merge and the single-issue
    # fix result -- already resolved via RESOLVED=yes with no FILE section --
    # stands unchanged.
    out = run_problem_solving(
        llm=MagicMock(),
        task=_task(),
        review_result=_review_result([_issue()]),
        current_files={"a.py": "old"},
        tool_agents={ToolAgentKind.DOCUMENTATION: tool_agent},
    )
    tool_agent.problem_solve.assert_called_once()
    assert out.resolved is True
    assert out.files == {"a.py": "old"}


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
    assert out.files == {"a.py": "code"}
    assert out.summary == "No actionable issues."


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
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    msgs = []
    out = run_problem_solving_for_microtask(
        llm=MagicMock(),
        microtask=_microtask(),
        review_result=_review_result([_issue()]),
        current_files={"a.py": "old"},
        detail_callback=msgs.append,
    )
    assert out.resolved is True
    assert out.files["a.py"] == "fixed"
    assert out.unresolved_issues == []
    assert "applied 1 fix" in out.summary
    assert msgs  # callback was invoked
    assert any("issue" in m.lower() for m in msgs)


def _phase_result(issues, phase_name="code_review"):
    from software_engineering_team.backend_code_v2_team.models import PhaseReviewResult

    return PhaseReviewResult(passed=False, issues=issues, phase_name=phase_name)


def test_run_phase_fixes_via_code_review():
    """run_code_review_fixes routes through internal _run_phase_fixes, which
    short-circuits on its no-actionable-issue path here: the supplied
    severity='info' issue is filtered out before the per-issue fix loop runs.
    See test_run_code_review_fixes_with_actionable for the actionable path.
    """
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
    assert out.files == {"a.py": "code"}
    assert out.summary == "No actionable code_review issues."


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


def test_run_qa_fixes_does_not_consult_tool_agent_problem_solve(monkeypatch):
    """QA is review-only: even when a QA tool agent is wired and the issue is
    actionable, fixing must come entirely from the generic coding-agent fix
    loop, never from the reviewer's own problem_solve."""
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_qa_fixes,
    )

    resp = (
        "## FILE a.py ##\nfixed\n"
        "## RESOLVED ##\nyes\n## END RESOLVED ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    tool_agent = MagicMock()

    out = run_qa_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        phase_result=_phase_result([_issue(severity="high", source="qa")], phase_name="qa"),
        current_files={"a.py": "code"},
        tool_agents={ToolAgentKind.TESTING_QA: tool_agent},
    )
    tool_agent.problem_solve.assert_not_called()
    assert out.resolved is True
    assert out.files["a.py"] == "fixed"


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


def test_run_security_fixes_does_not_consult_tool_agent_problem_solve(monkeypatch):
    """Security is review-only: even when a Security tool agent is wired and
    the issue is actionable, fixing must come entirely from the generic
    coding-agent fix loop, never from the reviewer's own problem_solve."""
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_security_fixes,
    )

    resp = (
        "## FILE a.py ##\nfixed\n"
        "## RESOLVED ##\nyes\n## END RESOLVED ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    tool_agent = MagicMock()

    out = run_security_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        phase_result=_phase_result(
            [_issue(severity="high", source="security")], phase_name="security"
        ),
        current_files={"a.py": "code"},
        tool_agents={ToolAgentKind.SECURITY: tool_agent},
    )
    tool_agent.problem_solve.assert_not_called()
    assert out.resolved is True
    assert out.files["a.py"] == "fixed"


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
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = run_code_review_fixes(
        llm=MagicMock(),
        microtask=_microtask(),
        phase_result=_phase_result([_issue(severity="high")]),
        current_files={"a.py": "old"},
    )
    assert isinstance(out.summary, str)


def test_run_code_review_fixes_tool_agent_raises(monkeypatch, caplog):
    """A BUILD_SPECIALIST problem_solve failure must mark the result
    unresolved and note the failure in the summary, without discarding the
    generic fix loop's already-applied file changes."""
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
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
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    tool_agent = MagicMock()
    tool_agent.problem_solve.side_effect = RuntimeError("build specialist crash")

    with caplog.at_level(logging.ERROR, logger=ps_mod.logger.name):
        out = run_code_review_fixes(
            llm=MagicMock(),
            microtask=_microtask(),
            phase_result=_phase_result([_issue(severity="high")]),
            current_files={"a.py": "old"},
            tool_agents={ToolAgentKind.BUILD_SPECIALIST: tool_agent},
            task_id="t-1",
        )

    # Generic fix loop's own progress must survive the tool-agent failure.
    assert out.files["a.py"] == "fixed"
    # But the overall phase result must now be reported as unresolved.
    assert out.resolved is False
    assert "tool-agent fix pass failed" in out.summary
    assert "build specialist crash" in out.summary
    assert any(
        "code_review" in r.getMessage() and "build specialist crash" in r.getMessage()
        for r in caplog.records
    )


def test_run_documentation_fixes_tool_agent_raises(monkeypatch, caplog):
    """Same contract as the code_review case, for the DOCUMENTATION tool agent."""
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.phases import problem_solving as ps_mod
    from software_engineering_team.backend_code_v2_team.phases.problem_solving import (
        run_documentation_fixes,
    )

    resp = (
        "## FILE a.py ##\nfixed\n"
        "## RESOLVED ##\nyes\n## END RESOLVED ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(ps_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(ps_mod, "resolve_text_mode_strands_model", lambda llm: object())

    tool_agent = MagicMock()
    tool_agent.problem_solve.side_effect = RuntimeError("doc agent crash")

    with caplog.at_level(logging.ERROR, logger=ps_mod.logger.name):
        out = run_documentation_fixes(
            llm=MagicMock(),
            microtask=_microtask(),
            phase_result=_phase_result(
                [_issue(severity="high", source="documentation")], phase_name="documentation"
            ),
            current_files={"a.py": "old"},
            tool_agents={ToolAgentKind.DOCUMENTATION: tool_agent},
            task_id="t-2",
        )

    assert out.files["a.py"] == "fixed"
    assert out.resolved is False
    assert "tool-agent fix pass failed" in out.summary
    assert "doc agent crash" in out.summary
    assert any(
        "documentation" in r.getMessage() and "doc agent crash" in r.getMessage()
        for r in caplog.records
    )

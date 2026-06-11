"""Tests for frontend_code_v2_team.phases.review.run_review and helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def _task(**overrides):
    from software_engineering_team.shared.models import Task, TaskType

    base = dict(
        id="t1",
        type=TaskType.FRONTEND,
        title="T",
        description="desc",
        requirements="reqs",
        assignee="frontend",
        acceptance_criteria=["AC"],
    )
    base.update(overrides)
    return Task(**base)


def _execution_result(files):
    from software_engineering_team.frontend_code_v2_team.models import ExecutionResult

    return ExecutionResult(files=files)


class _StubAgent:
    def __init__(self, response):
        self.response = response

    def __call__(self, prompt):
        return self.response


def test_fe_run_build_verification_no_verifier():
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        _run_build_verification,
    )

    ok, msg = _run_build_verification(Path("/tmp"), None, "t1")
    assert ok is True


def test_fe_run_build_verification_raises():
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        _run_build_verification,
    )

    def _bad(*a, **kw):
        raise RuntimeError("build crash")

    ok, msg = _run_build_verification(Path("/tmp"), _bad, "t1")
    assert ok is False
    assert "build crash" in msg


def test_fe_run_llm_review(monkeypatch):
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_llm_review

    resp = (
        "## PASSED ##\nfalse\n## END PASSED ##\n"
        "## ISSUES ##\n"
        "description: bad\nseverity: high\nfile_path: x.ts\nsource: code_review\n"
        "## END ISSUES ##\n"
        "## SUMMARY ##\nbad\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    issues = _run_llm_review(llm=MagicMock(), task=_task(), files={"x.ts": "code"})
    assert len(issues) == 1


def test_fe_run_review_clean(monkeypatch, tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    resp = "## PASSED ##\ntrue\n## END PASSED ##\n## ISSUES ##\n## END ISSUES ##\n## SUMMARY ##\nok\n## END SUMMARY ##\n"
    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        build_verifier=lambda *a, **kw: (True, ""),
    )
    assert out.passed


def test_fe_run_review_build_fails(monkeypatch, tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n"))
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        build_verifier=lambda *a, **kw: (False, "build err"),
    )
    assert out.build_ok is False
    assert any(i.source == "build" for i in out.issues)


def test_fe_run_review_with_qa_agent(monkeypatch, tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n"))
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    qa_agent = MagicMock()

    class _Bug:
        severity = "low"
        description = "x"
        location = "x.ts"
        recommendation = "fix"

    qa_agent.run.return_value = MagicMock(bugs_found=[_Bug()])

    out = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        qa_agent=qa_agent,
    )
    assert any(i.source == "qa" for i in out.issues)


def test_fe_run_review_with_linting_failures(monkeypatch, tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n"))
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    class _Issue:
        severity = "error"
        message = "missing semicolon"
        file_path = "x.ts"

    lint_agent = MagicMock()
    lint_agent.run.return_value = MagicMock(
        execution_result=MagicMock(success=False),
        passed=False,
        linter_issues=[_Issue()],
    )

    out = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        linting_tool_agent=lint_agent,
    )
    assert out.lint_ok is False


def test_fe_run_review_with_security_agent(monkeypatch, tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n"))
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    sec_agent = MagicMock()

    class _V:
        severity = "high"
        description = "XSS"
        location = "x.ts"
        recommendation = "sanitize"

    sec_agent.run.return_value = MagicMock(vulnerabilities=[_V()])

    out = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        security_agent=sec_agent,
    )
    assert any(i.source == "security" for i in out.issues)


def test_fe_run_review_with_code_review_agent(monkeypatch, tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n"))
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    cr_agent = MagicMock()

    class _Issue:
        severity = "medium"
        description = "x"
        file_path = "x.ts"
        recommendation = "fix"

    cr_agent.run.return_value = MagicMock(issues=[_Issue()])

    out = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
    )
    assert any(i.source == "code_review" for i in out.issues)


def test_fe_run_review_passes_files_dict_unmodified(monkeypatch, tmp_path: Path):
    """The code review agent receives ``files=`` verbatim — no 60K slice, no
    ``--- path ---`` concatenation."""
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n"))
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    captured: dict = {}

    def _capture(inp, **kw):
        captured["files"] = inp.files
        captured["code"] = inp.code
        return MagicMock(issues=[])

    cr_agent = MagicMock()
    cr_agent.run.side_effect = _capture

    files = {"big.ts": "x" * 100_000, "small.ts": "const y = 1;"}
    run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result(files),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
    )
    assert captured["files"] == files
    assert captured["code"] == ""


def test_fe_run_review_code_review_agent_raises_falls_back_to_llm(monkeypatch, tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod,
        "Agent",
        lambda *a, **kw: _StubAgent(
            "## PASSED ##\nfalse\n## END PASSED ##\n"
            "## ISSUES ##\ndescription: bad\nsource: code_review\n## END ISSUES ##\n"
            "## SUMMARY ##\nbad\n## END SUMMARY ##\n"
        ),
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    cr_agent = MagicMock()
    cr_agent.run.side_effect = RuntimeError("crash")

    out = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
    )
    assert any(i.source == "code_review" for i in out.issues)


def test_fe_run_review_with_tool_agents(monkeypatch, tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.models import (
        ReviewIssue,
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n"))
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    tool_agent = MagicMock()
    tool_agent.review.return_value = ToolAgentPhaseOutput(
        issues=[ReviewIssue(source="tool_a11y", severity="low", description="missing alt")],
        recommendations=["add alt text"],
    )

    out = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        tool_agents={ToolAgentKind.ACCESSIBILITY: tool_agent},
    )
    assert any("missing alt" in i.description for i in out.issues)

"""Tests for backend_code_v2_team.phases.review.run_review and helpers."""

from __future__ import annotations

from pathlib import Path
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
        acceptance_criteria=["AC"],
    )
    base.update(overrides)
    return Task(**base)


def _execution_result(files):
    from software_engineering_team.backend_code_v2_team.models import ExecutionResult

    return ExecutionResult(files=files)


class _StubAgent:
    def __init__(self, response):
        self.response = response

    def __call__(self, prompt):
        return self.response


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def test_run_llm_review_parses_issues(monkeypatch):
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import _run_llm_review

    resp = (
        "## PASSED ##\nfalse\n## END PASSED ##\n"
        "## ISSUES ##\n"
        "description: bad code\nseverity: high\nfile_path: x.py\nsource: code_review\n"
        "## END ISSUES ##\n"
        "## SUMMARY ##\nbad\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    issues = _run_llm_review(llm=MagicMock(), task=_task(), files={"x.py": "code"})
    assert len(issues) == 1


def test_run_llm_review_chunks_large_file_without_dropping_tail(monkeypatch):
    """A file larger than one prompt budget is reviewed in function-aware
    chunks, so its tail is seen by the reviewer instead of being truncated."""
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import (
        MAX_REVIEW_CODE_CHARS,
        _run_llm_review,
    )

    prompts: list[str] = []
    clean = (
        "## PASSED ##\ntrue\n## END PASSED ##\n"
        "## ISSUES ##\n## END ISSUES ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )

    class _RecordingAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            prompts.append(prompt)
            return clean

    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _RecordingAgent())
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    big = "\n".join(f"def fn_{i:04d}():\n    return {i}" for i in range(2500))
    assert len(big) > MAX_REVIEW_CODE_CHARS  # forces more than one chunk

    _run_llm_review(llm=MagicMock(), task=_task(), files={"big.py": big})

    assert len(prompts) > 1  # chunked, not a single truncated call
    joined = "\n".join(prompts)
    assert "fn_0000" in joined  # head reviewed
    assert "fn_2499" in joined  # tail reviewed — old truncation dropped this


def test_run_llm_review_skips_failing_chunk_keeps_others(monkeypatch):
    """A chunk whose LLM call raises is logged and skipped; issues from the
    other chunks survive instead of the whole review aborting."""
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import (
        MAX_REVIEW_CODE_CHARS,
        _run_llm_review,
    )

    good = (
        "## PASSED ##\nfalse\n## END PASSED ##\n"
        "## ISSUES ##\n"
        "description: real issue\nseverity: high\nfile_path: big.py\nsource: code_review\n"
        "## END ISSUES ##\n"
        "## SUMMARY ##\nbad\n## END SUMMARY ##\n"
    )
    calls = {"n": 0}

    class _FlakyAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("model unavailable")
            return good

    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _FlakyAgent())
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())
    # Force the "many chunks" warning path too, so a large review logs visibly.
    monkeypatch.setattr(review_mod, "MANY_CHUNKS_WARN_THRESHOLD", 0)

    big = "\n".join(f"def fn_{i:04d}():\n    return {i}" for i in range(2500))
    assert len(big) > MAX_REVIEW_CODE_CHARS  # forces more than one chunk

    issues = _run_llm_review(llm=MagicMock(), task=_task(), files={"big.py": big})

    assert calls["n"] > 1  # every chunk was attempted
    assert len(issues) >= 1  # the failed first chunk did not abort the review
    assert all(i.description == "real issue" for i in issues)


def test_run_build_verification_no_verifier():
    from software_engineering_team.backend_code_v2_team.phases.review import _run_build_verification

    ok, msg = _run_build_verification(Path("/tmp"), None, "t1")
    assert ok is True


def test_run_build_verification_raises():
    from software_engineering_team.backend_code_v2_team.phases.review import _run_build_verification

    def _bad(*a, **kw):
        raise RuntimeError("build crash")

    ok, msg = _run_build_verification(Path("/tmp"), _bad, "t1")
    assert ok is False
    assert "build crash" in msg


def test_run_build_verification_failure():
    from software_engineering_team.backend_code_v2_team.phases.review import _run_build_verification

    ok, msg = _run_build_verification(Path("/tmp"), lambda *a, **kw: (False, "err"), "t1")
    assert ok is False
    assert msg == "err"


# ---------------------------------------------------------------------------
# run_review
# ---------------------------------------------------------------------------


def test_run_review_clean(monkeypatch, tmp_path: Path):
    """No issues path: build passes, LLM review returns clean."""
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import run_review

    resp = "## PASSED ##\ntrue\n## END PASSED ##\n## ISSUES ##\n## END ISSUES ##\n## SUMMARY ##\nok\n## END SUMMARY ##\n"
    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        build_verifier=lambda *a, **kw: (True, ""),
    )
    assert result.passed
    assert result.build_ok


def test_run_review_build_fails(monkeypatch, tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        build_verifier=lambda *a, **kw: (False, "compile error"),
    )
    assert result.passed is False
    assert result.build_ok is False
    assert any(i.source == "build" for i in result.issues)


def test_run_review_with_external_qa_agent(monkeypatch, tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    qa_agent = MagicMock()

    class _Bug:
        severity = "low"
        description = "test"
        location = "x.py"
        recommendation = "fix"

    qa_agent.run.return_value = MagicMock(bugs_found=[_Bug()])

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        qa_agent=qa_agent,
    )
    assert any(i.source == "qa" for i in result.issues)


def test_run_review_qa_agent_raises(monkeypatch, tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    qa_agent = MagicMock()
    qa_agent.run.side_effect = RuntimeError("qa crashed")

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        qa_agent=qa_agent,
    )
    # Should not raise; just continues
    assert result is not None


def test_run_review_with_security_agent(monkeypatch, tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    sec_agent = MagicMock()

    class _Vuln:
        severity = "high"
        description = "SQL injection"
        location = "x.py"
        recommendation = "parameterize"

    sec_agent.run.return_value = MagicMock(vulnerabilities=[_Vuln()])

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        security_agent=sec_agent,
    )
    assert any(i.source == "security" for i in result.issues)


def test_run_review_with_code_review_agent(monkeypatch, tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    cr_agent = MagicMock()

    class _Issue:
        severity = "medium"
        description = "magic number"
        file_path = "x.py"
        recommendation = "use constant"

    cr_agent.run.return_value = MagicMock(issues=[_Issue()])

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
    )
    assert any(i.source == "code_review" for i in result.issues)


def test_run_review_code_review_agent_raises_falls_back_to_llm(monkeypatch, tmp_path: Path):
    """If code_review_agent fails, we still call LLM fallback."""
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import run_review

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

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
    )
    # LLM fallback was called
    assert any(i.source == "code_review" for i in result.issues)


def test_run_review_with_linting_agent_pass(monkeypatch, tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    lint_agent = MagicMock()
    lint_agent.run.return_value = MagicMock(
        execution_result=MagicMock(success=True),
        passed=True,
        linter_issues=[],
    )

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        linting_tool_agent=lint_agent,
    )
    assert result.lint_ok


def test_run_review_with_linting_agent_failures(monkeypatch, tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    class _LintIssue:
        severity = "error"
        message = "syntax problem"
        file_path = "x.py"

    lint_agent = MagicMock()
    lint_agent.run.return_value = MagicMock(
        execution_result=MagicMock(success=False),
        passed=False,
        linter_issues=[_LintIssue()],
    )

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        linting_tool_agent=lint_agent,
    )
    assert result.lint_ok is False
    assert any(i.source == "lint" for i in result.issues)


def test_run_review_with_tool_agents(monkeypatch, tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    tool_agent = MagicMock()

    class _Issue:
        source = "custom"
        severity = "low"
        description = "x"
        file_path = ""
        recommendation = ""

    tool_agent.review.return_value = ToolAgentPhaseOutput(
        issues=[
            review_mod.ReviewIssue(
                source="tool_security",
                severity="low",
                description="from tool agent",
                recommendation="ok",
            )
        ],
        recommendations=["add tests"],
    )

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        tool_agents={ToolAgentKind.SECURITY: tool_agent},
    )
    assert any("tool agent" in i.description for i in result.issues)
    # recommendations were added as info issues
    assert any("add tests" in i.description for i in result.issues)


def test_run_review_tool_agent_raises(monkeypatch, tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    tool_agent = MagicMock()
    tool_agent.review.side_effect = RuntimeError("err")

    # Should not raise
    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        tool_agents={ToolAgentKind.TESTING_QA: tool_agent},
    )
    assert result is not None


def test_run_review_tool_agent_without_review_method(monkeypatch, tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.phases import review as review_mod
    from software_engineering_team.backend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    bare = object()  # no .review method
    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        tool_agents={ToolAgentKind.TESTING_QA: bare},
    )
    assert result is not None

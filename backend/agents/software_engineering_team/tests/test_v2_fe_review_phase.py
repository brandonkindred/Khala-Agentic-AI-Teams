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


def test_fe_run_llm_review_chunks_large_file_without_dropping_tail(monkeypatch):
    """The frontend fallback splits a too-large file into function-aware chunks,
    so its tail is reviewed instead of truncated (mirrors the backend test)."""
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import (
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

    big = "\n".join(f"function fn_{i:04d}() {{\n  return {i};\n}}" for i in range(2500))
    assert len(big) > MAX_REVIEW_CODE_CHARS  # forces more than one chunk

    _run_llm_review(llm=MagicMock(), task=_task(), files={"big.ts": big})

    assert len(prompts) > 1  # chunked, not a single truncated call
    joined = "\n".join(prompts)
    assert "fn_0000" in joined  # head reviewed
    assert "fn_2499" in joined  # tail reviewed — old truncation dropped this


def test_fe_run_llm_review_skips_failing_chunk_keeps_others(monkeypatch):
    """A failing chunk is logged and skipped; the frontend review keeps the
    other chunks' issues (mirrors the backend resilience test)."""
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        MAX_REVIEW_CODE_CHARS,
        _run_llm_review,
    )

    good = (
        "## PASSED ##\nfalse\n## END PASSED ##\n"
        "## ISSUES ##\n"
        "description: real issue\nseverity: high\nfile_path: big.ts\nsource: code_review\n"
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
    monkeypatch.setattr(review_mod, "MANY_CHUNKS_WARN_THRESHOLD", 0)

    big = "\n".join(f"function fn_{i:04d}() {{\n  return {i};\n}}" for i in range(2500))
    assert len(big) > MAX_REVIEW_CODE_CHARS  # forces more than one chunk

    issues = _run_llm_review(llm=MagicMock(), task=_task(), files={"big.ts": big})

    assert calls["n"] > 1
    assert len(issues) >= 1
    assert all(i.description == "real issue" for i in issues)


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

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
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

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
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

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
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

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
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


class _Bug:
    severity = "low"
    description = "real bug"
    location = "big.py"
    recommendation = ""


class _Vuln:
    severity = "high"
    description = "real vuln"
    location = "big.py"
    recommendation = ""


def _big_source() -> str:
    """A single file larger than one QA/security prompt budget."""
    from software_engineering_team.frontend_code_v2_team.phases.review import MAX_REVIEW_CODE_CHARS

    big = "\n".join(f"def fn_{i:04d}():\n    return {i}" for i in range(2500))
    assert len(big) > MAX_REVIEW_CODE_CHARS  # forces more than one chunk
    return big


def test_fe_run_qa_agent_chunks_large_input_without_dropping_tail():
    """The QA agent is run once per function-aware chunk, so a large file's
    tail is reviewed instead of being truncated at MAX_REVIEW_CODE_CHARS."""
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_qa_agent

    codes: list[str] = []

    class _QAAgent:
        def run(self, inp):
            codes.append(inp.code)
            return MagicMock(bugs_found=[_Bug()])

    issues = _run_qa_agent(
        qa_agent=_QAAgent(),
        files={"big.py": _big_source()},
        language="typescript",
        task_description="t",
        task_id="t1",
    )

    assert len(codes) > 1  # one QA call per chunk, not a single truncated call
    joined = "\n".join(codes)
    assert "fn_0000" in joined  # head reviewed
    assert "fn_2499" in joined  # tail reviewed — old 60K cap dropped this
    assert len(issues) == len(codes)
    assert all(i.source == "qa" for i in issues)


def test_fe_run_qa_agent_skips_failing_chunk_keeps_others(monkeypatch):
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_qa_agent

    calls = {"n": 0}

    class _QAAgent:
        def run(self, inp):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("qa unavailable")
            return MagicMock(bugs_found=[_Bug()])

    monkeypatch.setattr(review_mod, "MANY_CHUNKS_WARN_THRESHOLD", 0)

    issues = _run_qa_agent(
        qa_agent=_QAAgent(),
        files={"big.py": _big_source()},
        language="typescript",
        task_description="t",
        task_id="t1",
    )
    assert calls["n"] > 1  # every chunk attempted
    assert len(issues) >= 1  # failed first chunk did not abort the review
    assert all(i.description == "real bug" for i in issues)


def test_fe_run_security_agent_chunks_large_input_without_dropping_tail():
    """The security agent is run once per function-aware chunk, covering the
    whole file instead of only the first MAX_REVIEW_CODE_CHARS."""
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_security_agent

    codes: list[str] = []

    class _SecAgent:
        def run(self, inp):
            codes.append(inp.code)
            return MagicMock(vulnerabilities=[_Vuln()])

    issues = _run_security_agent(
        security_agent=_SecAgent(),
        files={"big.py": _big_source()},
        language="typescript",
        task_description="t",
        task_id="t1",
    )

    assert len(codes) > 1
    joined = "\n".join(codes)
    assert "fn_0000" in joined  # head reviewed
    assert "fn_2499" in joined  # tail reviewed
    assert len(issues) == len(codes)
    assert all(i.source == "security" for i in issues)


def test_fe_run_security_agent_single_call_for_small_input():
    """Inputs that already fit are reviewed in one call (no regression)."""
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_security_agent

    calls = {"n": 0}

    class _SecAgent:
        def run(self, inp):
            calls["n"] += 1
            return MagicMock(vulnerabilities=[_Vuln()])

    issues = _run_security_agent(
        security_agent=_SecAgent(),
        files={"x.ts": "const f = () => 1;"},
        language="typescript",
        task_description="t",
        task_id="t1",
    )
    assert calls["n"] == 1
    assert len(issues) == 1


def test_fe_run_review_with_code_review_agent(monkeypatch, tmp_path: Path):
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
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

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
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

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
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

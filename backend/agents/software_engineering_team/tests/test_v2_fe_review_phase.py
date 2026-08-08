"""Tests for frontend_code_v2_team.phases.review.run_review and helpers."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

# A reviewer-rendered original-line-number prefix ("123: code"); raw source has none.
_LINE_NUM_PREFIX = re.compile(r"^\d+: ", re.MULTILINE)


def _assert_raw_source(codes: list[str]) -> None:
    """Every piece fed to QA/security is raw source — no ``### path ###`` header
    and no ``N:`` line-number prefixes (those are code-review-prompt artifacts
    that would make the source syntactically invalid)."""
    assert all("### " not in c for c in codes)
    assert not any(_LINE_NUM_PREFIX.search(c) for c in codes)


def _task(**overrides):
    from shared.dev_models.models import Task, TaskType

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


def test_fe_run_build_verification_uses_profile_label():
    from software_engineering_team.frontend_code_v2_team.phases._profile import PROFILE
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        _run_build_verification,
    )

    seen = {}

    def _verifier(repo_path, label, task_id):
        seen["label"] = label
        return True, ""

    _run_build_verification(Path("/tmp"), _verifier, "t1")
    assert seen["label"] == PROFILE.build_verify_label


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

    out = _run_llm_review(llm=MagicMock(), task=_task(), files={"x.ts": "code"})
    assert len(out.issues) == 1


def test_fe_run_llm_review_accepts_language_kwarg(monkeypatch):
    """The shared ``llm_review_fn`` contract now always passes ``language``
    (see ``shared.v2_review._code_review_step``); the frontend fallback must
    accept it without raising ``TypeError`` even though it does not use it --
    this team's ``REVIEW_PROMPT`` has no language placeholder and its code is
    always TypeScript."""
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_llm_review

    resp = (
        "## PASSED ##\ntrue\n## END PASSED ##\n"
        "## ISSUES ##\n## END ISSUES ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )
    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _StubAgent(resp))
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    out = _run_llm_review(
        llm=MagicMock(), task=_task(), files={"x.ts": "code"}, language="typescript"
    )
    assert out.issues == []


def test_fe_run_llm_review_forwards_review_context(monkeypatch):
    """The LLM fallback reviewer also sees architecture/spec_content (mirrors the
    backend regression test — previously only the external agent path received
    this context)."""
    from llm_service.clients.dummy import DummyLLMClient
    from shared.dev_models.models import ReviewContext, SystemArchitecture
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_llm_review

    prompts: list[str] = []

    class _RecordingAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            prompts.append(prompt)
            return "## PASSED ##\ntrue\n## END PASSED ##\n## ISSUES ##\n## END ISSUES ##\n## SUMMARY ##\nok\n## END SUMMARY ##\n"

    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _RecordingAgent())
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    architecture = SystemArchitecture(overview="Layered service architecture.")
    review_context = ReviewContext(
        architecture=architecture, spec_content="All endpoints require auth."
    )

    _run_llm_review(
        llm=DummyLLMClient(),
        task=_task(),
        files={"x.ts": "code"},
        review_context=review_context,
    )
    assert "Layered service architecture." in prompts[0]
    assert "All endpoints require auth." in prompts[0]


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

    out = _run_llm_review(llm=MagicMock(), task=_task(), files={"big.ts": big})

    assert calls["n"] > 1
    assert len(out.issues) >= 1
    assert all(i.description == "real issue" for i in out.issues)


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
    """Mock QA finding (a bug) for the chunked-review tests."""

    severity = "low"
    description = "real bug"
    location = "big.py"
    recommendation = ""


class _Vuln:
    """Mock security finding (a vulnerability) for the chunked-review tests."""

    severity = "high"
    description = "real vuln"
    location = "big.py"
    recommendation = ""


def _big_source() -> str:
    """A single file larger than one QA/security prompt budget.

    Grows whole functions until the source exceeds MAX_REVIEW_CODE_CHARS, so the
    test adapts automatically if that constant changes. The source begins with
    fn_0000 (head) and ends with a fn_tail sentinel (tail); tests assert both
    survive chunking, proving neither end is dropped.
    """
    from software_engineering_team.frontend_code_v2_team.phases.review import MAX_REVIEW_CODE_CHARS

    lines: list[str] = []
    total = 0
    i = 0
    while total <= MAX_REVIEW_CODE_CHARS:
        fn = f"def fn_{i:04d}():\n    return {i}"
        lines.append(fn)
        total += len(fn) + 1  # +1 for the joining newline
        i += 1
    lines.append("def fn_tail():\n    return -1")
    big = "\n".join(lines)
    assert len(big) > MAX_REVIEW_CODE_CHARS  # forces more than one chunk
    return big


def test_fe_run_qa_agent_chunks_large_input_without_dropping_tail():
    """The QA agent is run once per raw piece of a large file, so its tail is
    reviewed instead of being truncated at MAX_REVIEW_CODE_CHARS — and the code
    sent is raw source, not the code-review renderer's headers/line prefixes."""
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

    assert len(codes) > 1  # one QA call per piece, not a single truncated call
    joined = "\n".join(codes)
    assert "fn_0000" in joined  # head reviewed
    assert "fn_tail" in joined  # tail reviewed — old 60K cap dropped this
    assert codes[0].startswith("def fn_0000")  # raw source, no ### header / prefix
    _assert_raw_source(codes)
    # Function-aware: every piece begins at a function boundary, never mid-body.
    assert all(c.lstrip().startswith("def ") for c in codes)
    assert len(issues) == len(codes)
    assert all(i.source == "qa" for i in issues)


def test_fe_run_qa_agent_skips_failing_chunk_keeps_others(monkeypatch):
    """A chunk whose QA call raises is skipped; issues from the others survive."""
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
    """The security agent is run once per raw piece, covering the whole file
    instead of only the first MAX_REVIEW_CODE_CHARS, on raw source."""
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
    assert "fn_tail" in joined  # tail reviewed
    assert codes[0].startswith("def fn_0000")  # raw source, no ### header / prefix
    _assert_raw_source(codes)
    # Function-aware: every piece begins at a function boundary, never mid-body.
    assert all(c.lstrip().startswith("def ") for c in codes)
    assert len(issues) == len(codes)
    assert all(i.source == "security" for i in issues)


def _oversized_single_line() -> str:
    """A single source line longer than the per-call budget.

    A minified bundle / long one-line data literal that ``build_review_chunks``
    cannot split at a line boundary, so it returns one over-budget chunk. The
    review paths must hard-split it at character boundaries before any agent call.
    """
    from software_engineering_team.frontend_code_v2_team.phases.review import MAX_REVIEW_CODE_CHARS

    line = "const DATA='" + ("a" * (MAX_REVIEW_CODE_CHARS + 5_000)) + "';"
    assert "\n" not in line  # unsplittable at a line boundary
    assert len(line) > MAX_REVIEW_CODE_CHARS
    return line


def test_fe_run_qa_agent_hard_splits_oversized_single_line():
    """A single line over the cap is hard-split so every QA call stays within
    budget and the file is reviewed instead of sent in one oversized, skippable call."""
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        MAX_REVIEW_CODE_CHARS,
        _run_qa_agent,
    )

    codes: list[str] = []

    class _QAAgent:
        def run(self, inp):
            codes.append(inp.code)
            return MagicMock(bugs_found=[_Bug()])

    issues = _run_qa_agent(
        qa_agent=_QAAgent(),
        files={"bundle.js": _oversized_single_line()},
        language="typescript",
        task_description="t",
        task_id="t1",
    )

    assert len(codes) > 1  # hard-split, not one oversized call
    assert all(len(c) <= MAX_REVIEW_CODE_CHARS for c in codes)  # every piece bounded
    # Raw source: the pieces concatenate back to exactly the original line.
    assert "".join(codes) == _oversized_single_line()
    _assert_raw_source(codes)
    assert len(issues) == len(codes)
    assert all(i.source == "qa" for i in issues)


def test_fe_run_security_agent_hard_splits_oversized_single_line():
    """Same hard-split guarantee for the security agent path."""
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        MAX_REVIEW_CODE_CHARS,
        _run_security_agent,
    )

    codes: list[str] = []

    class _SecAgent:
        def run(self, inp):
            codes.append(inp.code)
            return MagicMock(vulnerabilities=[_Vuln()])

    issues = _run_security_agent(
        security_agent=_SecAgent(),
        files={"bundle.js": _oversized_single_line()},
        language="typescript",
        task_description="t",
        task_id="t1",
    )

    assert len(codes) > 1
    assert all(len(c) <= MAX_REVIEW_CODE_CHARS for c in codes)
    assert "".join(codes) == _oversized_single_line()  # raw, reconstructs exactly
    _assert_raw_source(codes)
    assert all(i.source == "security" for i in issues)


def test_fe_run_qa_agent_defaults_file_path_to_sent_file():
    """When the QA item reports no location, the finding is attributed to the
    file actually sent — so even tail pieces stay attributable."""
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_qa_agent

    class _NoLocBug:
        severity = "low"
        description = "bug"
        location = None  # agent did not localize the finding

    class _QAAgent:
        def run(self, inp):
            return MagicMock(bugs_found=[_NoLocBug()])

    issues = _run_qa_agent(
        qa_agent=_QAAgent(),
        files={"src/widget.ts": "const x = 1;"},
        language="typescript",
        task_description="t",
        task_id="t1",
    )
    assert issues and all(i.file_path == "src/widget.ts" for i in issues)


def test_fe_run_llm_review_hard_splits_oversized_single_line(monkeypatch):
    """The LLM fallback also hard-splits an oversized single line so the file is
    not sent in one prompt that may overflow the context and be skipped — and
    every piece keeps the ### path ### header so tail findings stay attributable."""
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import _run_llm_review

    codes: list[str] = []
    clean = (
        "## PASSED ##\ntrue\n## END PASSED ##\n"
        "## ISSUES ##\n## END ISSUES ##\n"
        "## SUMMARY ##\nok\n## END SUMMARY ##\n"
    )

    class _RecordingAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            codes.append(prompt)
            return clean

    monkeypatch.setattr(review_mod, "Agent", lambda *a, **kw: _RecordingAgent())
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    full = _oversized_single_line()
    _run_llm_review(llm=MagicMock(), task=_task(), files={"bundle.js": full})

    assert len(codes) > 1  # the oversized line was hard-split across prompts
    # The whole oversized line never fits in any single prompt — it was split.
    assert not any(full in prompt for prompt in codes)
    # Every piece keeps the file header, so a finding in any tail piece is
    # still attributable to the file (the gap Codex flagged).
    assert all("### bundle.js ###" in prompt for prompt in codes)


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


def test_fe_run_review_forwards_architecture_and_spec_content(monkeypatch, tmp_path: Path):
    """``run_review``'s ``architecture``/``spec_content`` reach the code-review
    agent's input, and default to ``None``/``""`` when omitted."""
    from shared.dev_models.models import ReviewContext, SystemArchitecture
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    captured: dict = {}

    def _capture(inp, **kw):
        captured["architecture"] = inp.architecture
        captured["spec_content"] = inp.spec_content
        return MagicMock(issues=[])

    cr_agent = MagicMock()
    cr_agent.run.side_effect = _capture
    architecture = SystemArchitecture(overview="layered architecture")

    run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
        review_context=ReviewContext(
            architecture=architecture, spec_content="the full project spec"
        ),
    )
    assert captured["architecture"] is architecture
    assert captured["spec_content"] == "the full project spec"

    cr_agent.run.reset_mock(side_effect=True)
    cr_agent.run.side_effect = _capture
    run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
    )
    assert captured["architecture"] is None
    assert captured["spec_content"] == ""


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


# ---------------------------------------------------------------------------
# Concurrent review fan-out (code review / QA / security)
# ---------------------------------------------------------------------------


def test_fe_review_steps_run_sequentially_for_dummy_llm():
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        _review_steps_run_sequentially,
    )

    assert _review_steps_run_sequentially(DummyLLMClient()) is True
    assert _review_steps_run_sequentially(MagicMock()) is False


def test_fe_review_steps_run_sequentially_for_wrapped_dummy_llm():
    """The coding team's default llm_getter wraps clients in a Strands LLMClientModel
    (exposing the backing client via a `.client` property) before they reach review.py — a
    DummyLLMClient reached only through that wrapper must still force sequential execution."""
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        _review_steps_run_sequentially,
    )

    class _FakeStrandsModelWrapper:
        def __init__(self, client):
            self.client = client

    assert _review_steps_run_sequentially(_FakeStrandsModelWrapper(DummyLLMClient())) is True
    assert _review_steps_run_sequentially(_FakeStrandsModelWrapper(MagicMock())) is False


def test_fe_run_review_steps_run_concurrently(monkeypatch, tmp_path: Path):
    """code_review/QA/security must fan out — a 3-way barrier only releases if all three
    run in parallel worker threads; a sequential loop would deadlock and time out."""
    import threading

    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    barrier = threading.Barrier(3, timeout=30)

    code_review_agent = MagicMock()

    def _cr_run(inp):
        barrier.wait()
        return MagicMock(issues=[])

    code_review_agent.run.side_effect = _cr_run

    qa_agent = MagicMock()

    def _qa_run(inp):
        barrier.wait()
        return MagicMock(bugs_found=[])

    qa_agent.run.side_effect = _qa_run

    security_agent = MagicMock()

    def _sec_run(inp):
        barrier.wait()
        return MagicMock(vulnerabilities=[])

    security_agent.run.side_effect = _sec_run

    # llm is a plain MagicMock (not a DummyLLMClient), so the fan-out is not forced sequential.
    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        code_review_agent=code_review_agent,
        qa_agent=qa_agent,
        security_agent=security_agent,
    )

    assert (
        result is not None
    )  # the barrier releasing (rather than timing out) is the real assertion


def test_fe_run_review_qa_failure_does_not_drop_other_steps_issues(monkeypatch, tmp_path: Path):
    """A QA step that fails outright (bypassing the shared per-chunk containment inside
    ``_run_qa_agent``) must not swallow the code-review/security findings collected in the
    same fan-out — each step's failure is contained to a synthetic issue for that step alone."""
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    def _boom(**_kw):
        raise RuntimeError("qa exploded outright")

    monkeypatch.setattr(review_mod, "_run_qa_agent", _boom)

    class _Vuln:
        severity = "critical"
        description = "sec issue"
        location = "x.ts"
        recommendation = "fix"

    security_agent = MagicMock()
    security_agent.run.return_value = MagicMock(vulnerabilities=[_Vuln()])

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        qa_agent=MagicMock(),
        security_agent=security_agent,
    )

    assert any(i.source == "security" and i.description == "sec issue" for i in result.issues)
    assert any(i.source == "qa" and i.severity == "high" for i in result.issues)


def test_fe_run_review_security_failure_does_not_drop_other_steps_issues(
    monkeypatch, tmp_path: Path
):
    """A security step that fails outright (bypassing the shared per-chunk containment inside
    ``_run_security_agent``) must not swallow the code-review/QA findings collected in the same
    fan-out — each step's failure is contained to a synthetic issue for that step alone."""
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    monkeypatch.setattr(
        review_mod, "Agent", lambda *a, **kw: _StubAgent("## PASSED ##\ntrue\n## END PASSED ##\n")
    )
    monkeypatch.setattr(review_mod, "resolve_text_mode_strands_model", lambda llm: object())

    def _boom(**_kw):
        raise RuntimeError("security exploded outright")

    monkeypatch.setattr(review_mod, "_run_security_agent", _boom)

    class _Bug:
        severity = "low"
        description = "qa issue"
        location = "x.ts"
        recommendation = "fix"

    qa_agent = MagicMock()
    qa_agent.run.return_value = MagicMock(bugs_found=[_Bug()])

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        qa_agent=qa_agent,
        security_agent=MagicMock(),
    )

    assert any(i.source == "qa" and i.description == "qa issue" for i in result.issues)
    assert any(i.source == "security" and i.severity == "critical" for i in result.issues)


def test_fe_run_review_code_review_llm_fallback_failure_does_not_drop_other_steps_issues(
    monkeypatch, tmp_path: Path
):
    """The LLM fallback inside _code_review_step (used when there is no code_review_agent, or
    when the external agent itself fails) must be guarded too — an outright failure there is
    reported as a synthetic issue rather than propagating and cancelling the QA/security steps
    still running in the same fan-out."""
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import run_review

    def _boom(**_kw):
        raise RuntimeError("llm fallback exploded")

    monkeypatch.setattr(review_mod, "_run_llm_review", _boom)

    class _Bug:
        severity = "low"
        description = "qa issue"
        location = "x.ts"
        recommendation = "fix"

    qa_agent = MagicMock()
    qa_agent.run.return_value = MagicMock(bugs_found=[_Bug()])

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.ts": "code"}),
        repo_path=tmp_path,
        qa_agent=qa_agent,
        # No code_review_agent: run_review falls straight into the guarded LLM fallback.
    )

    assert any(i.source == "qa" and i.description == "qa issue" for i in result.issues)
    assert any(i.source == "code_review" and i.severity == "high" for i in result.issues)


# ---------------------------------------------------------------------------
# Phase-specific testing gates (run_code_review_phase / run_qa_testing_phase /
# run_security_testing_phase)
# ---------------------------------------------------------------------------


def test_fe_run_code_review_phase_code_review_failure_is_contained(monkeypatch, tmp_path: Path):
    """A critical code-review finding fails the standalone code-review gate — driven
    solely by the code-review step, with no build/lint step involved."""
    from software_engineering_team.frontend_code_v2_team.models import Microtask
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        run_code_review_phase,
    )
    from software_engineering_team.shared.llm_review import LlmReviewOutput
    from software_engineering_team.shared.v2_models import ReviewIssue

    monkeypatch.setattr(
        review_mod,
        "_run_llm_review",
        lambda **_kw: LlmReviewOutput(
            issues=[
                ReviewIssue(
                    source="code_review",
                    severity="critical",
                    description="unsafe innerHTML usage",
                )
            ],
            raw_issue_count=1,
        ),
    )

    result = run_code_review_phase(
        llm=MagicMock(),
        task=_task(),
        microtask=Microtask(id="mt-1"),
        repo_path=tmp_path,
        files={"x.ts": "code"},
    )

    assert result.passed is False
    assert result.phase_name == "code_review"
    assert any(
        i.source == "code_review"
        and i.severity == "critical"
        and "unsafe innerHTML" in i.description
        for i in result.issues
    )


def test_fe_run_qa_testing_phase_agent_failure_is_contained(monkeypatch):
    """An outright QA-agent failure inside the standalone testing-phase gate must not
    propagate — it becomes a synthetic high-severity issue instead of raising, mirroring
    ``_qa_review_step``'s identical containment in the fan-out path.

    ``_run_qa_agent`` itself is patched (rather than ``qa_agent.run``) because
    ``run_chunked_agent_review`` already catches and skips a per-chunk ``qa_agent.run``
    failure — the guard under test here is for a failure in the step itself, not a chunk.
    """
    from software_engineering_team.frontend_code_v2_team.models import Microtask
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        run_qa_testing_phase,
    )

    def _boom(**_kw):
        raise RuntimeError("qa agent exploded")

    monkeypatch.setattr(review_mod, "_run_qa_agent", _boom)

    result = run_qa_testing_phase(
        task=_task(),
        microtask=Microtask(id="mt-1"),
        files={"x.ts": "code"},
        qa_agent=MagicMock(),
    )

    assert result.passed is False
    assert any(
        i.source == "qa" and i.severity == "high" and "qa agent exploded" in i.description
        for i in result.issues
    )


def test_fe_run_security_testing_phase_agent_failure_is_contained(monkeypatch):
    """An outright security-agent failure inside the standalone testing-phase gate must not
    propagate — it becomes a synthetic critical-severity issue instead of raising, mirroring
    ``_security_review_step``'s identical containment in the fan-out path. See
    ``test_fe_run_qa_testing_phase_agent_failure_is_contained`` for why the step function
    itself (not ``security_agent.run``) is patched."""
    from software_engineering_team.frontend_code_v2_team.models import Microtask
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        run_security_testing_phase,
    )

    def _boom(**_kw):
        raise RuntimeError("security agent exploded")

    monkeypatch.setattr(review_mod, "_run_security_agent", _boom)

    result = run_security_testing_phase(
        task=_task(),
        microtask=Microtask(id="mt-1"),
        files={"x.ts": "code"},
        security_agent=MagicMock(),
    )

    assert result.passed is False
    assert any(
        i.source == "security"
        and i.severity == "critical"
        and "security agent exploded" in i.description
        for i in result.issues
    )


def test_fe_run_code_review_phase_passes_when_clean(monkeypatch, tmp_path: Path):
    """Empty LLM review → passed with no issues."""
    from software_engineering_team.frontend_code_v2_team.models import Microtask
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        run_code_review_phase,
    )
    from software_engineering_team.shared.llm_review import LlmReviewOutput

    monkeypatch.setattr(
        review_mod,
        "_run_llm_review",
        lambda **_kw: LlmReviewOutput(issues=[], raw_issue_count=0),
    )

    result = run_code_review_phase(
        llm=MagicMock(),
        task=_task(),
        microtask=Microtask(id="mt-1"),
        repo_path=tmp_path,
        files={"x.ts": "code"},
    )

    assert result.passed is True
    assert result.phase_name == "code_review"
    assert result.issues == []


def test_fe_run_qa_testing_phase_passes_when_clean(monkeypatch):
    """Successful QA agent with no findings → passed with no issues."""
    from software_engineering_team.frontend_code_v2_team.models import Microtask
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        run_qa_testing_phase,
    )

    monkeypatch.setattr(review_mod, "_run_qa_agent", lambda **_kw: [])

    result = run_qa_testing_phase(
        task=_task(),
        microtask=Microtask(id="mt-1"),
        files={"x.ts": "code"},
        qa_agent=MagicMock(),
    )

    assert result.passed is True
    assert result.phase_name == "qa"
    assert result.issues == []


def test_fe_run_security_testing_phase_passes_when_clean(monkeypatch):
    """Successful security agent with no findings → passed with no issues."""
    from software_engineering_team.frontend_code_v2_team.models import Microtask
    from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
    from software_engineering_team.frontend_code_v2_team.phases.review import (
        run_security_testing_phase,
    )

    monkeypatch.setattr(review_mod, "_run_security_agent", lambda **_kw: [])

    result = run_security_testing_phase(
        task=_task(),
        microtask=Microtask(id="mt-1"),
        files={"x.ts": "code"},
        security_agent=MagicMock(),
    )

    assert result.passed is True
    assert result.phase_name == "security"
    assert result.issues == []

"""Tests for backend_code_v2_team.phases._profile.run_review and helpers (shared.v2_review_bindings)."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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
    from software_engineering_team.codegen_team.models import ExecutionResult

    return ExecutionResult(files=files)


def _stub_coordinator(monkeypatch, *, approved: bool = True, issues=None) -> None:
    """Patch ``review_mod.run_coordinator`` to return a canned ``CodeReviewOutput``.

    Mirrors the old "## PASSED ## true" stub-``Agent`` pattern: a clean pass
    with no issues by default, for tests exercising QA/security/build/
    tool-agent orchestration rather than code-review content itself.
    """
    from software_engineering_team.code_review_agent.models import CodeReviewOutput
    from software_engineering_team.shared import v2_review_bindings as review_mod

    def _stub(llm, input_data, *args, **kwargs):
        return CodeReviewOutput(
            approved=approved, issues=issues or [], summary="stub", spec_compliance_notes=""
        )

    monkeypatch.setattr(review_mod, "run_coordinator", _stub)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def test_run_llm_review_calls_coordinator_with_skip_tail_passes(monkeypatch):
    """The fallback calls the shared coordinator directly, in lightweight mode
    (no tail passes), threading task/files through to CodeReviewInput."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_llm_review
    from software_engineering_team.code_review_agent.models import CodeReviewOutput
    from software_engineering_team.shared import v2_review_bindings as review_mod

    captured: dict = {}

    def _spy(llm, input_data, *args, **kwargs):
        captured["input_data"] = input_data
        return CodeReviewOutput(approved=True, issues=[])

    monkeypatch.setattr(review_mod, "run_coordinator", _spy)

    task = _task(requirements="reqs", acceptance_criteria=["AC1", "AC2"])
    _run_llm_review(llm=MagicMock(), task=task, files={"x.py": "code"})

    input_data = captured["input_data"]
    assert input_data.skip_tail_passes is True
    assert input_data.files == {"x.py": "code"}
    assert input_data.task_description == task.description
    assert input_data.task_requirements == "reqs"
    assert input_data.acceptance_criteria == ["AC1", "AC2"]


def test_run_llm_review_defaults_language_to_profile_default(monkeypatch):
    """With no ``language`` argument, the fallback must still set
    ``CodeReviewInput.language`` to this team's own default ("python") rather
    than leaving it unset and falling back to ``CodeReviewInput``'s own
    "typescript" default -- backend code reviewed under the wrong language
    label would misdirect the LLM's language-specific checks."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_llm_review
    from software_engineering_team.code_review_agent.models import CodeReviewOutput
    from software_engineering_team.shared import v2_review_bindings as review_mod

    captured: dict = {}

    def _spy(llm, input_data, *args, **kwargs):
        captured["input_data"] = input_data
        return CodeReviewOutput(approved=True, issues=[])

    monkeypatch.setattr(review_mod, "run_coordinator", _spy)

    _run_llm_review(llm=MagicMock(), task=_task(), files={"x.py": "code"})

    assert captured["input_data"].language == "python"


def test_run_llm_review_forwards_explicit_language(monkeypatch):
    """An explicit ``language`` argument reaches ``CodeReviewInput`` verbatim,
    matching the external ``code_review_agent`` path's handling in
    ``shared.v2_review._code_review_step``."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_llm_review
    from software_engineering_team.code_review_agent.models import CodeReviewOutput
    from software_engineering_team.shared import v2_review_bindings as review_mod

    captured: dict = {}

    def _spy(llm, input_data, *args, **kwargs):
        captured["input_data"] = input_data
        return CodeReviewOutput(approved=True, issues=[])

    monkeypatch.setattr(review_mod, "run_coordinator", _spy)

    _run_llm_review(llm=MagicMock(), task=_task(), files={"x.py": "code"}, language="go")

    assert captured["input_data"].language == "go"


def test_run_llm_review_forwards_review_context(monkeypatch):
    """The LLM fallback reviewer (used when no external code_review_agent is
    configured, or it fails) also sees architecture/spec_content -- previously
    only the external agent path received this context, so a deployment
    relying on the fallback never saw it despite callers passing review_context."""
    from shared.dev_models.models import ReviewContext, SystemArchitecture
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_llm_review
    from software_engineering_team.code_review_agent.models import CodeReviewOutput
    from software_engineering_team.shared import v2_review_bindings as review_mod

    captured: dict = {}

    def _spy(llm, input_data, *args, **kwargs):
        captured["input_data"] = input_data
        return CodeReviewOutput(approved=True, issues=[])

    monkeypatch.setattr(review_mod, "run_coordinator", _spy)

    architecture = SystemArchitecture(overview="Layered service architecture.")
    review_context = ReviewContext(
        architecture=architecture, spec_content="All endpoints require auth."
    )

    _run_llm_review(
        llm=MagicMock(),
        task=_task(),
        files={"x.py": "code"},
        review_context=review_context,
    )
    input_data = captured["input_data"]
    assert input_data.architecture is architecture
    assert input_data.spec_content == "All endpoints require auth."


def test_run_llm_review_translates_issues_to_review_issue(monkeypatch):
    """CodeReviewIssue fields translate to ReviewIssue: suggestion -> recommendation,
    source is set to "code_review", and raw_issue_count is always None (the
    lightweight coordinator has no grounding pass to report a pre-filter count
    for; reporting a fabricated int would make review_cycle.py's grounding
    circuit breaker see a false "0% rejected" instead of "no data")."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_llm_review
    from software_engineering_team.code_review_agent.models import (
        CodeReviewIssue,
        CodeReviewOutput,
    )
    from software_engineering_team.shared import v2_review_bindings as review_mod

    monkeypatch.setattr(
        review_mod,
        "run_coordinator",
        lambda llm, input_data, *a, **kw: CodeReviewOutput(
            approved=False,
            issues=[
                CodeReviewIssue(
                    severity="high",
                    category="logic",
                    file_path="x.py",
                    description="bad code",
                    suggestion="fix it",
                )
            ],
        ),
    )

    out = _run_llm_review(llm=MagicMock(), task=_task(), files={"x.py": "code"})

    assert out.raw_issue_count is None
    assert len(out.issues) == 1
    issue = out.issues[0]
    assert issue.source == "code_review"
    assert issue.severity == "high"
    assert issue.description == "bad code"
    assert issue.file_path == "x.py"
    assert issue.recommendation == "fix it"


def test_run_llm_review_raw_issue_count_is_none_on_clean_pass(monkeypatch):
    """raw_issue_count stays None even when the coordinator finds nothing --
    it must never default back to 0, which grounding_rejection_ratio would
    also treat as "no ratio available" today but could stop doing so."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_llm_review
    from software_engineering_team.code_review_agent.models import CodeReviewOutput
    from software_engineering_team.shared import v2_review_bindings as review_mod

    monkeypatch.setattr(
        review_mod,
        "run_coordinator",
        lambda llm, input_data, *a, **kw: CodeReviewOutput(approved=True, issues=[]),
    )

    out = _run_llm_review(llm=MagicMock(), task=_task(), files={"x.py": "code"})

    assert out.issues == []
    assert out.raw_issue_count is None


def test_run_llm_review_propagates_coordinator_unavailable(monkeypatch):
    """A total coordinator failure (no chunk reviewable) is not swallowed here --
    it propagates so the caller's containment produces the fail-closed synthetic
    issue instead of a silent clean pass."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_llm_review
    from software_engineering_team.code_review_agent.models import CodeReviewUnavailableError
    from software_engineering_team.shared import v2_review_bindings as review_mod

    def _raise(llm, input_data, *a, **kw):
        raise CodeReviewUnavailableError("no chunk could be reviewed", unreviewed=[])

    monkeypatch.setattr(review_mod, "run_coordinator", _raise)

    with pytest.raises(CodeReviewUnavailableError):
        _run_llm_review(llm=MagicMock(), task=_task(), files={"x.py": "code"})


def test_run_build_verification_no_verifier():
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        _run_build_verification,
    )

    ok, msg = _run_build_verification(Path("/tmp"), None, "t1")
    assert ok is True


def test_run_build_verification_raises():
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        _run_build_verification,
    )

    def _bad(*a, **kw):
        raise RuntimeError("build crash")

    ok, msg = _run_build_verification(Path("/tmp"), _bad, "t1")
    assert ok is False
    assert "build crash" in msg


def test_run_build_verification_failure():
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        _run_build_verification,
    )

    ok, msg = _run_build_verification(Path("/tmp"), lambda *a, **kw: (False, "err"), "t1")
    assert ok is False
    assert msg == "err"


def test_run_build_verification_uses_profile_label():
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        PROFILE,
        _run_build_verification,
    )

    seen = {}

    def _verifier(repo_path, label, task_id):
        seen["label"] = label
        return True, ""

    _run_build_verification(Path("/tmp"), _verifier, "t1")
    assert seen["label"] == PROFILE.build_verify_label


# ---------------------------------------------------------------------------
# run_review
# ---------------------------------------------------------------------------


def test_run_review_clean(monkeypatch, tmp_path: Path):
    """No issues path: build passes, LLM review returns clean."""
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

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
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

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
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

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
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

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
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

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
    from software_engineering_team.shared.review_utils import MAX_REVIEW_CODE_CHARS

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


def _oversized_single_line() -> str:
    """A single source line longer than the per-call budget.

    A minified bundle / long one-line data literal that ``build_review_chunks``
    cannot split at a line boundary, so it returns one over-budget chunk. The
    review paths must hard-split it at character boundaries before any agent call.
    """
    from software_engineering_team.shared.review_utils import MAX_REVIEW_CODE_CHARS

    line = "DATA = '" + ("a" * (MAX_REVIEW_CODE_CHARS + 5_000)) + "'"
    assert "\n" not in line  # unsplittable at a line boundary
    assert len(line) > MAX_REVIEW_CODE_CHARS
    return line


def test_run_qa_agent_hard_splits_oversized_single_line():
    """A single line over the cap is hard-split so every QA call stays within
    budget and the file is reviewed instead of sent in one oversized, skippable call."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_qa_agent
    from software_engineering_team.shared.review_utils import MAX_REVIEW_CODE_CHARS

    codes: list[str] = []

    class _QAAgent:
        def run(self, inp):
            codes.append(inp.code)
            return MagicMock(bugs_found=[_Bug()])

    issues = _run_qa_agent(
        qa_agent=_QAAgent(),
        files={"bundle.py": _oversized_single_line()},
        language="python",
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


def test_run_security_agent_hard_splits_oversized_single_line():
    """Same hard-split guarantee for the security agent path."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_security_agent
    from software_engineering_team.shared.review_utils import MAX_REVIEW_CODE_CHARS

    codes: list[str] = []

    class _SecAgent:
        def run(self, inp):
            codes.append(inp.code)
            return MagicMock(vulnerabilities=[_Vuln()])

    issues = _run_security_agent(
        security_agent=_SecAgent(),
        files={"bundle.py": _oversized_single_line()},
        language="python",
        task_description="t",
        task_id="t1",
    )

    assert len(codes) > 1
    assert all(len(c) <= MAX_REVIEW_CODE_CHARS for c in codes)
    assert "".join(codes) == _oversized_single_line()  # raw, reconstructs exactly
    _assert_raw_source(codes)
    assert all(i.source == "security" for i in issues)


def test_run_qa_agent_defaults_file_path_to_sent_file():
    """When the QA item reports no location, the finding is attributed to the
    file actually sent — so even tail pieces stay attributable."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_qa_agent

    class _NoLocBug:
        severity = "low"
        description = "bug"
        location = None  # agent did not localize the finding

    class _QAAgent:
        def run(self, inp):
            return MagicMock(bugs_found=[_NoLocBug()])

    issues = _run_qa_agent(
        qa_agent=_QAAgent(),
        files={"app/svc.py": "def f():\n    return 1"},
        language="python",
        task_description="t",
        task_id="t1",
    )
    assert issues and all(i.file_path == "app/svc.py" for i in issues)


def test_run_qa_agent_chunks_large_input_without_dropping_tail():
    """The QA agent is run once per raw piece of a large file, so its tail is
    reviewed instead of being truncated at MAX_REVIEW_CODE_CHARS — and the code
    sent is raw source, not the code-review renderer's headers/line prefixes."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_qa_agent

    codes: list[str] = []

    class _QAAgent:
        def run(self, inp):
            codes.append(inp.code)
            return MagicMock(bugs_found=[_Bug()])

    issues = _run_qa_agent(
        qa_agent=_QAAgent(),
        files={"big.py": _big_source()},
        language="python",
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
    assert len(issues) == len(codes)  # a bug aggregated from every piece
    assert all(i.source == "qa" for i in issues)


def test_run_qa_agent_single_call_for_small_input():
    """Inputs that already fit are reviewed in one call (no regression)."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_qa_agent

    calls = {"n": 0}

    class _QAAgent:
        def run(self, inp):
            calls["n"] += 1
            return MagicMock(bugs_found=[_Bug()])

    issues = _run_qa_agent(
        qa_agent=_QAAgent(),
        files={"x.py": "def f():\n    return 1"},
        language="python",
        task_description="t",
        task_id="t1",
    )
    assert calls["n"] == 1
    assert len(issues) == 1


def test_run_qa_agent_skips_failing_chunk_keeps_others(monkeypatch):
    """A chunk whose QA call raises is skipped; issues from the others survive."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_qa_agent
    from software_engineering_team.shared import v2_review_bindings as review_mod

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
        language="python",
        task_description="t",
        task_id="t1",
    )
    assert calls["n"] > 1  # every chunk attempted
    assert len(issues) >= 1  # failed first chunk did not abort the review
    assert all(i.description == "real bug" for i in issues)


def test_run_security_agent_chunks_large_input_without_dropping_tail():
    """The security agent is run once per raw piece, covering the whole file
    instead of only the first MAX_REVIEW_CODE_CHARS, on raw source."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_security_agent

    codes: list[str] = []

    class _SecAgent:
        def run(self, inp):
            codes.append(inp.code)
            return MagicMock(vulnerabilities=[_Vuln()])

    issues = _run_security_agent(
        security_agent=_SecAgent(),
        files={"big.py": _big_source()},
        language="python",
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


def test_run_security_agent_skips_failing_chunk_keeps_others(monkeypatch):
    """A chunk whose security call raises is skipped; the others' issues survive."""
    from software_engineering_team.codegen_team.stacks.backend.profile import _run_security_agent
    from software_engineering_team.shared import v2_review_bindings as review_mod

    calls = {"n": 0}

    class _SecAgent:
        def run(self, inp):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("security unavailable")
            return MagicMock(vulnerabilities=[_Vuln()])

    monkeypatch.setattr(review_mod, "MANY_CHUNKS_WARN_THRESHOLD", 0)

    issues = _run_security_agent(
        security_agent=_SecAgent(),
        files={"big.py": _big_source()},
        language="python",
        task_description="t",
        task_id="t1",
    )
    assert calls["n"] > 1
    assert len(issues) >= 1
    assert all(i.description == "real vuln" for i in issues)


def test_run_review_with_code_review_agent(monkeypatch, tmp_path: Path):
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

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


def test_run_review_passes_files_dict_unmodified(monkeypatch, tmp_path: Path):
    """The code review agent receives ``files=`` verbatim — no 60K slice, no
    ``--- path ---`` concatenation."""
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

    captured: dict = {}

    def _capture(inp, **kw):
        captured["files"] = inp.files
        return MagicMock(issues=[])

    cr_agent = MagicMock()
    cr_agent.run.side_effect = _capture

    # A file well over the legacy 60K cap proves the dict is not sliced.
    files = {"big.py": "x" * 100_000, "small.py": "y = 1"}
    run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result(files),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
    )
    assert captured["files"] == files


def test_run_review_forwards_architecture_and_spec_content(monkeypatch, tmp_path: Path):
    """``run_review``'s ``architecture``/``spec_content`` reach the code-review
    agent's input, and default to ``None``/``""`` when omitted."""
    from shared.dev_models.models import ReviewContext, SystemArchitecture
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

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
        execution_result=_execution_result({"x.py": "code"}),
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
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
    )
    assert captured["architecture"] is None
    assert captured["spec_content"] == ""


def test_run_review_code_review_agent_raises_falls_back_to_llm(monkeypatch, tmp_path: Path):
    """If code_review_agent fails, we still call LLM fallback."""
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review
    from software_engineering_team.code_review_agent.models import CodeReviewIssue

    _stub_coordinator(
        monkeypatch,
        approved=False,
        issues=[CodeReviewIssue(severity="high", description="bad", suggestion="")],
    )

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
    """A linting agent reporting success/passed=True yields lint_ok=True."""
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

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
    """A linting agent reporting failure yields lint_ok=False and a "lint"-sourced issue."""
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

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
    """A tool agent's issues and recommendations both surface in the result."""
    from software_engineering_team.codegen_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review
    from software_engineering_team.shared import v2_review_bindings as review_mod

    _stub_coordinator(monkeypatch)

    tool_agent = MagicMock()

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
    """A tool agent whose .review() raises is contained -- run_review still returns."""
    from software_engineering_team.codegen_team.models import ToolAgentKind
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

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
    """A tool agent lacking a .review() method entirely is skipped, not a crash."""
    from software_engineering_team.codegen_team.models import ToolAgentKind
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

    bare = object()  # no .review method
    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        tool_agents={ToolAgentKind.TESTING_QA: bare},
    )
    assert result is not None


# ---------------------------------------------------------------------------
# Concurrent review fan-out (code review / QA / security)
# ---------------------------------------------------------------------------


def test_review_steps_run_sequentially_for_dummy_llm():
    """A DummyLLMClient (scripted, not thread-safe) forces sequential review steps;
    any other client allows the concurrent fan-out."""
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.shared.v2_review import _review_steps_run_sequentially

    assert _review_steps_run_sequentially(DummyLLMClient()) is True
    assert _review_steps_run_sequentially(MagicMock()) is False


def test_review_steps_run_sequentially_for_wrapped_dummy_llm():
    """The coding team's default llm_getter wraps clients in a Strands LLMClientModel
    (exposing the backing client via a `.client` property) before they reach review.py — a
    DummyLLMClient reached only through that wrapper must still force sequential execution."""
    from llm_service.clients.dummy import DummyLLMClient
    from software_engineering_team.shared.v2_review import _review_steps_run_sequentially

    class _FakeStrandsModelWrapper:
        def __init__(self, client):
            self.client = client

    assert _review_steps_run_sequentially(_FakeStrandsModelWrapper(DummyLLMClient())) is True
    assert _review_steps_run_sequentially(_FakeStrandsModelWrapper(MagicMock())) is False


def test_run_review_steps_run_concurrently(monkeypatch, tmp_path: Path):
    """code_review/QA/security must fan out — a 3-way barrier only releases if all three
    run in parallel worker threads; a sequential loop would deadlock and time out."""
    import threading

    from software_engineering_team.codegen_team.stacks.backend.profile import run_review

    _stub_coordinator(monkeypatch)

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
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        code_review_agent=code_review_agent,
        qa_agent=qa_agent,
        security_agent=security_agent,
    )

    assert (
        result is not None
    )  # the barrier releasing (rather than timing out) is the real assertion


def test_run_review_qa_failure_does_not_drop_other_steps_issues(monkeypatch, tmp_path: Path):
    """A QA step that fails outright (bypassing the shared per-chunk containment inside
    ``_run_qa_agent``) must not swallow the code-review/security findings collected in the
    same fan-out — each step's failure is contained to a synthetic issue for that step alone."""
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review
    from software_engineering_team.shared import v2_review_bindings as review_mod

    _stub_coordinator(monkeypatch)

    def _boom(**_kw):
        raise RuntimeError("qa exploded outright")

    monkeypatch.setattr(review_mod, "_run_qa_agent", _boom)

    class _Vuln:
        severity = "critical"
        description = "sec issue"
        location = "x.py"
        recommendation = "fix"

    security_agent = MagicMock()
    security_agent.run.return_value = MagicMock(vulnerabilities=[_Vuln()])

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        qa_agent=MagicMock(),
        security_agent=security_agent,
    )

    assert any(i.source == "security" and i.description == "sec issue" for i in result.issues)
    assert any(i.source == "qa" and i.severity == "high" for i in result.issues)


def test_run_review_security_failure_does_not_drop_other_steps_issues(monkeypatch, tmp_path: Path):
    """A security step that fails outright (bypassing the shared per-chunk containment inside
    ``_run_security_agent``) must not swallow the code-review/QA findings collected in the same
    fan-out — each step's failure is contained to a synthetic issue for that step alone."""
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review
    from software_engineering_team.shared import v2_review_bindings as review_mod

    _stub_coordinator(monkeypatch)

    def _boom(**_kw):
        raise RuntimeError("security exploded outright")

    monkeypatch.setattr(review_mod, "_run_security_agent", _boom)

    class _Bug:
        severity = "low"
        description = "qa issue"
        location = "x.py"
        recommendation = "fix"

    qa_agent = MagicMock()
    qa_agent.run.return_value = MagicMock(bugs_found=[_Bug()])

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        qa_agent=qa_agent,
        security_agent=MagicMock(),
    )

    assert any(i.source == "qa" and i.description == "qa issue" for i in result.issues)
    assert any(i.source == "security" and i.severity == "critical" for i in result.issues)


def test_run_review_code_review_llm_fallback_failure_does_not_drop_other_steps_issues(
    monkeypatch, tmp_path: Path
):
    """The LLM fallback inside _code_review_step (used when there is no code_review_agent, or
    when the external agent itself fails) must be guarded too — an outright failure there is
    reported as a synthetic issue rather than propagating and cancelling the QA/security steps
    still running in the same fan-out."""
    from software_engineering_team.codegen_team.stacks.backend.profile import run_review
    from software_engineering_team.shared import v2_review_bindings as review_mod

    def _boom(**_kw):
        raise RuntimeError("llm fallback exploded")

    monkeypatch.setattr(review_mod, "_run_llm_review", _boom)

    class _Bug:
        severity = "low"
        description = "qa issue"
        location = "x.py"
        recommendation = "fix"

    qa_agent = MagicMock()
    qa_agent.run.return_value = MagicMock(bugs_found=[_Bug()])

    result = run_review(
        llm=MagicMock(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        qa_agent=qa_agent,
        # No code_review_agent: run_review falls straight into the guarded LLM fallback.
    )

    assert any(i.source == "qa" and i.description == "qa issue" for i in result.issues)
    assert any(i.source == "code_review" and i.severity == "high" for i in result.issues)


# ---------------------------------------------------------------------------
# Phase-specific testing gates (run_qa_testing_phase / run_security_testing_phase)
# ---------------------------------------------------------------------------


def test_run_qa_testing_phase_agent_failure_is_contained(monkeypatch):
    """An outright QA-agent failure inside the standalone testing-phase gate must not
    propagate — it becomes a synthetic high-severity issue instead of raising, mirroring
    ``_qa_review_step``'s identical containment in the fan-out path.

    ``_run_qa_agent`` itself is patched (rather than ``qa_agent.run``) because
    ``run_chunked_agent_review`` already catches and skips a per-chunk ``qa_agent.run``
    failure — the guard under test here is for a failure in the step itself, not a chunk.
    """
    from software_engineering_team.codegen_team.models import Microtask
    from software_engineering_team.codegen_team.stacks.backend.profile import run_qa_testing_phase
    from software_engineering_team.shared import v2_review_bindings as review_mod

    def _boom(**_kw):
        raise RuntimeError("qa agent exploded")

    monkeypatch.setattr(review_mod, "_run_qa_agent", _boom)

    result = run_qa_testing_phase(
        task=_task(),
        microtask=Microtask(id="mt-1"),
        files={"x.py": "code"},
        qa_agent=MagicMock(),
    )

    assert result.passed is False
    assert any(
        i.source == "qa" and i.severity == "high" and "qa agent exploded" in i.description
        for i in result.issues
    )


def test_run_security_testing_phase_agent_failure_is_contained(monkeypatch):
    """An outright security-agent failure inside the standalone testing-phase gate must not
    propagate — it becomes a synthetic critical-severity issue instead of raising, mirroring
    ``_security_review_step``'s identical containment in the fan-out path. See
    ``test_run_qa_testing_phase_agent_failure_is_contained`` for why the step function itself
    (not ``security_agent.run``) is patched."""
    from software_engineering_team.codegen_team.models import Microtask
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        run_security_testing_phase,
    )
    from software_engineering_team.shared import v2_review_bindings as review_mod

    def _boom(**_kw):
        raise RuntimeError("security agent exploded")

    monkeypatch.setattr(review_mod, "_run_security_agent", _boom)

    result = run_security_testing_phase(
        task=_task(),
        microtask=Microtask(id="mt-1"),
        files={"x.py": "code"},
        security_agent=MagicMock(),
    )

    assert result.passed is False
    assert any(
        i.source == "security"
        and i.severity == "critical"
        and "security agent exploded" in i.description
        for i in result.issues
    )


def test_run_microtask_review_forwards_cache(monkeypatch, tmp_path: Path):
    """Optional ``cache`` reaches the QA step via ``agent_review_cache`` (parity with FE)."""
    from software_engineering_team.codegen_team.models import Microtask
    from software_engineering_team.codegen_team.stacks.backend.profile import run_microtask_review
    from software_engineering_team.shared import v2_review_bindings as review_mod
    from software_engineering_team.shared.agent_review import AgentReviewCache

    seen: dict = {}

    def _qa(**kw):
        seen["cache"] = kw.get("cache")
        return []

    monkeypatch.setattr(review_mod, "_run_qa_agent", _qa)
    monkeypatch.setattr(review_mod, "_run_security_agent", lambda **_kw: [])
    monkeypatch.setattr(
        review_mod,
        "_run_llm_review",
        lambda **_kw: MagicMock(issues=[], raw_issue_count=0),
    )

    cache = AgentReviewCache()
    run_microtask_review(
        llm=MagicMock(),
        task=_task(),
        microtask=Microtask(id="mt-1"),
        repo_path=tmp_path,
        files={"x.py": "code"},
        qa_agent=MagicMock(),
        cache=cache,
    )

    assert seen["cache"] is cache


def test_run_code_review_phase_passes_when_clean(monkeypatch, tmp_path: Path):
    """Empty LLM review → passed with no issues (backend counterpart to
    ``test_fe_run_code_review_phase_passes_when_clean``)."""
    from software_engineering_team.codegen_team.models import Microtask
    from software_engineering_team.codegen_team.stacks.backend.profile import run_code_review_phase
    from software_engineering_team.shared import v2_review_bindings as review_mod
    from software_engineering_team.shared.v2_review import LlmReviewOutput

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
        files={"x.py": "code"},
    )

    assert result.passed is True
    assert result.phase_name == "code_review"
    assert result.issues == []


def test_run_qa_testing_phase_passes_when_clean(monkeypatch):
    """Successful QA agent with no findings → passed with no issues (backend
    counterpart to ``test_fe_run_qa_testing_phase_passes_when_clean``)."""
    from software_engineering_team.codegen_team.models import Microtask
    from software_engineering_team.codegen_team.stacks.backend.profile import run_qa_testing_phase
    from software_engineering_team.shared import v2_review_bindings as review_mod

    monkeypatch.setattr(review_mod, "_run_qa_agent", lambda **_kw: [])

    result = run_qa_testing_phase(
        task=_task(),
        microtask=Microtask(id="mt-1"),
        files={"x.py": "code"},
        qa_agent=MagicMock(),
    )

    assert result.passed is True
    assert result.phase_name == "qa"
    assert result.issues == []


def test_run_security_testing_phase_passes_when_clean(monkeypatch):
    """Successful security agent with no findings → passed with no issues
    (backend counterpart to ``test_fe_run_security_testing_phase_passes_when_clean``)."""
    from software_engineering_team.codegen_team.models import Microtask
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        run_security_testing_phase,
    )
    from software_engineering_team.shared import v2_review_bindings as review_mod

    monkeypatch.setattr(review_mod, "_run_security_agent", lambda **_kw: [])

    result = run_security_testing_phase(
        task=_task(),
        microtask=Microtask(id="mt-1"),
        files={"x.py": "code"},
        security_agent=MagicMock(),
    )

    assert result.passed is True
    assert result.phase_name == "security"
    assert result.issues == []

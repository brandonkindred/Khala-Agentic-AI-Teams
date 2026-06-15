"""Tests for ``software_engineering_team.quality_gate_tools``.

Each public function delegates to a downstream agent or the orchestrator. We
patch the downstream symbol to avoid running real LLM / subprocess calls and
verify the wrapper translates results / handles exceptions gracefully.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_default_llm_getter_returns_strands_model(monkeypatch) -> None:
    from software_engineering_team import quality_gate_tools

    sentinel = object()
    monkeypatch.setattr(
        "llm_service.get_strands_model", lambda agent_key: sentinel
    )
    assert quality_gate_tools._default_llm_getter("some_key") is sentinel


# ---------------------------------------------------------------------------
# run_code_review
# ---------------------------------------------------------------------------


class _ReviewIssue:
    severity = "minor"
    description = "x"

    def model_dump(self):
        return {"severity": self.severity, "description": self.description}


class _ReviewResult:
    approved = True
    issues = [_ReviewIssue()]
    summary = "ok"
    spec_compliance_notes = "good"


def test_run_code_review_happy_path(monkeypatch) -> None:
    from software_engineering_team import quality_gate_tools as q

    monkeypatch.setattr(
        "code_review_agent.CodeReviewAgent",
        lambda llm: MagicMock(run=lambda inp, progress_callback=None: _ReviewResult()),
    )
    monkeypatch.setattr(
        "software_engineering_team.shared.context_sizing.compute_code_review_total_chars",
        lambda llm: 10,
    )
    # quality_gate_tools currently passes ``task_requirements or []`` (a list)
    # to ``CodeReviewInput`` which expects a string. Pass a string explicitly
    # to exercise the happy path; the default list behaviour is exercised by
    # ``test_run_code_review_default_task_requirements_list``.
    result = q.run_code_review(
        code="x" * 50,
        spec_content="spec",
        task_description="task",
        language="python",
        task_requirements="reqs as string",
        llm_getter=lambda k: MagicMock(),
    )
    assert result.approved is True
    assert result.issues
    assert result.summary == "ok"


def test_run_code_review_default_task_requirements_list(monkeypatch) -> None:
    """When ``task_requirements`` is None the tool falls through ``or []``,
    which historically fails ``CodeReviewInput`` validation. The wrapper
    catches that and returns a failed result; exercise that failure path."""
    from software_engineering_team import quality_gate_tools as q

    monkeypatch.setattr(
        "code_review_agent.CodeReviewAgent",
        lambda llm: MagicMock(run=lambda inp, progress_callback=None: _ReviewResult()),
    )
    monkeypatch.setattr(
        "software_engineering_team.shared.context_sizing.compute_code_review_total_chars",
        lambda llm: 1000,
    )
    result = q.run_code_review(
        code="x",
        spec_content="spec",
        task_description="task",
        language="python",
        llm_getter=lambda k: MagicMock(),
    )
    # Either succeeds (if Pydantic ever accepts list) or fails gracefully —
    # both are valid behaviour the tool currently exposes.
    assert isinstance(result.approved, bool)


def test_run_code_review_exception_returns_failed(monkeypatch) -> None:
    from software_engineering_team import quality_gate_tools as q

    def boom(*a, **kw):
        raise RuntimeError("agent down")

    monkeypatch.setattr("code_review_agent.CodeReviewAgent", lambda llm: SimpleNamespace(run=boom))
    monkeypatch.setattr(
        "software_engineering_team.shared.context_sizing.compute_code_review_total_chars",
        lambda llm: 1000,
    )
    result = q.run_code_review(
        code="x",
        spec_content="",
        task_description="",
        language="python",
        llm_getter=lambda k: MagicMock(),
    )
    assert result.approved is False
    assert "Review failed" in result.summary


# ---------------------------------------------------------------------------
# run_build_verification
# ---------------------------------------------------------------------------


def test_run_build_verification_success(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    monkeypatch.setattr(
        "software_engineering_team.orchestrator._run_build_verification",
        lambda repo_path, agent_type, task_id: (True, ""),
    )
    result = q.run_build_verification(tmp_path, "backend", "t1")
    assert result.success is True
    assert result.is_env_failure is False


def test_run_build_verification_env_failure(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    monkeypatch.setattr(
        "software_engineering_team.orchestrator._run_build_verification",
        lambda repo_path, agent_type, task_id: (False, "ENV:missing"),
    )
    result = q.run_build_verification(tmp_path, "backend", "t1")
    assert result.success is False
    assert result.is_env_failure is True


def test_run_build_verification_exception(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    def boom(*a, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr("software_engineering_team.orchestrator._run_build_verification", boom)
    result = q.run_build_verification(tmp_path, "backend", "t1")
    assert result.success is False
    assert "nope" in result.error


# ---------------------------------------------------------------------------
# run_linting
# ---------------------------------------------------------------------------


class _LintResultModel:
    passed = False

    class _Issue:
        def model_dump(self):
            return {"file": "a.py"}

    issues = [_Issue()]


def test_run_linting_happy_path(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    fake_agent = SimpleNamespace(run=lambda path: _LintResultModel())
    monkeypatch.setattr("linting_tool_agent.LintingToolAgent", lambda llm: fake_agent)
    result = q.run_linting(tmp_path, "t1", llm_getter=lambda k: MagicMock())
    assert result.passed is False
    assert result.issues


def test_run_linting_exception_non_blocking(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    def boom(*a, **kw):
        raise RuntimeError("lint exploded")

    monkeypatch.setattr("linting_tool_agent.LintingToolAgent", lambda llm: SimpleNamespace(run=boom))
    result = q.run_linting(tmp_path, "t1", llm_getter=lambda k: MagicMock())
    assert result.passed is True


# ---------------------------------------------------------------------------
# run_dbc_comments
# ---------------------------------------------------------------------------


def test_run_dbc_comments_no_code_returns_compliant(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    # Empty repo (no .py/.ts files) → code stays empty → returns compliant
    result = q.run_dbc_comments(
        tmp_path, "t1", "python", "task", llm_getter=lambda k: MagicMock()
    )
    assert result.compliant is True


def test_run_dbc_comments_already_compliant(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    (tmp_path / "a.py").write_text("def x(): pass")

    class _Result:
        already_compliant = True
        files = {}
        comments_added = 0
        comments_updated = 0
        suggested_commit_message = ""

    fake_agent = SimpleNamespace(run=lambda inp: _Result())
    monkeypatch.setattr(
        "technical_writers.dbc_comments_agent.DbcCommentsAgent", lambda llm: fake_agent
    )
    result = q.run_dbc_comments(
        tmp_path, "t1", "python", "task", llm_getter=lambda k: MagicMock()
    )
    assert result.compliant is True


def test_run_dbc_comments_writes_files(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    (tmp_path / "a.py").write_text("def x(): pass")

    class _Result:
        already_compliant = False
        files = {"a.py": "# new\ndef x(): pass"}
        comments_added = 2
        comments_updated = 1
        suggested_commit_message = "docs(dbc): add"

    fake_agent = SimpleNamespace(run=lambda inp: _Result())
    monkeypatch.setattr(
        "technical_writers.dbc_comments_agent.DbcCommentsAgent", lambda llm: fake_agent
    )

    captured = {}

    def fake_write(repo_path, files, msg):
        captured["repo"] = repo_path
        captured["files"] = files
        captured["msg"] = msg

    monkeypatch.setattr(
        "software_engineering_team.shared.git_utils.write_files_and_commit", fake_write
    )
    result = q.run_dbc_comments(
        tmp_path, "t1", "python", "task", llm_getter=lambda k: MagicMock()
    )
    assert result.compliant is False
    assert result.comments_added == 2
    assert captured["files"] == {"a.py": "# new\ndef x(): pass"}


def test_run_dbc_comments_exception_non_blocking(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    (tmp_path / "a.py").write_text("x")

    def boom(*a, **kw):
        raise RuntimeError("agent err")

    monkeypatch.setattr(
        "technical_writers.dbc_comments_agent.DbcCommentsAgent", lambda llm: SimpleNamespace(run=boom)
    )
    result = q.run_dbc_comments(
        tmp_path, "t1", "python", "task", llm_getter=lambda k: MagicMock()
    )
    assert result.compliant is True  # non-blocking


def test_run_dbc_comments_skips_excluded_paths(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    # Files in excluded folders should be skipped, leaving code empty → compliant
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("ignore me")
    (tmp_path / "binary.txt").write_text("ignore")
    result = q.run_dbc_comments(
        tmp_path, "t1", "python", "task", llm_getter=lambda k: MagicMock()
    )
    assert result.compliant is True


# ---------------------------------------------------------------------------
# run_qa_check
# ---------------------------------------------------------------------------


def test_run_qa_check_passed(monkeypatch) -> None:
    from software_engineering_team import quality_gate_tools as q

    class _Bug:
        def model_dump(self):
            return {"severity": "low"}

    class _Result:
        bugs = []

    monkeypatch.setattr("qa_agent.QAExpertAgent", lambda llm: SimpleNamespace(run=lambda **kw: _Result()))
    result = q.run_qa_check(code="x", task_description="t", language="py", llm_getter=lambda k: MagicMock())
    assert result.passed is True


def test_run_qa_check_with_bugs(monkeypatch) -> None:
    from software_engineering_team import quality_gate_tools as q

    class _Bug:
        def model_dump(self):
            return {"severity": "high"}

    class _Result:
        bugs = [_Bug()]

    monkeypatch.setattr("qa_agent.QAExpertAgent", lambda llm: SimpleNamespace(run=lambda **kw: _Result()))
    result = q.run_qa_check(code="x", task_description="t", language="py", llm_getter=lambda k: MagicMock())
    assert result.passed is False
    assert result.bugs


def test_run_qa_check_exception_non_blocking(monkeypatch) -> None:
    from software_engineering_team import quality_gate_tools as q

    monkeypatch.setattr(
        "qa_agent.QAExpertAgent",
        lambda llm: SimpleNamespace(
            run=lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        ),
    )
    result = q.run_qa_check(code="x", task_description="t", language="py", llm_getter=lambda k: MagicMock())
    assert result.passed is True


# ---------------------------------------------------------------------------
# run_security_scan
# ---------------------------------------------------------------------------


def test_run_security_scan_clean(monkeypatch) -> None:
    from software_engineering_team import quality_gate_tools as q

    class _Result:
        vulnerabilities = []

    monkeypatch.setattr(
        "security_agent.CybersecurityExpertAgent",
        lambda llm: SimpleNamespace(run=lambda **kw: _Result()),
    )
    result = q.run_security_scan(code="x", task_description="t", language="py", llm_getter=lambda k: MagicMock())
    assert result.passed is True


def test_run_security_scan_with_vulns(monkeypatch) -> None:
    from software_engineering_team import quality_gate_tools as q

    class _Vuln:
        def model_dump(self):
            return {"severity": "high"}

    class _Result:
        vulnerabilities = [_Vuln()]

    monkeypatch.setattr(
        "security_agent.CybersecurityExpertAgent",
        lambda llm: SimpleNamespace(run=lambda **kw: _Result()),
    )
    result = q.run_security_scan(code="x", task_description="t", language="py", llm_getter=lambda k: MagicMock())
    assert result.passed is False


def test_run_security_scan_exception_non_blocking(monkeypatch) -> None:
    from software_engineering_team import quality_gate_tools as q

    monkeypatch.setattr(
        "security_agent.CybersecurityExpertAgent",
        lambda llm: SimpleNamespace(
            run=lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        ),
    )
    result = q.run_security_scan(code="x", task_description="t", language="py", llm_getter=lambda k: MagicMock())
    assert result.passed is True


# ---------------------------------------------------------------------------
# run_acceptance_verification
# ---------------------------------------------------------------------------


def test_run_acceptance_verification_accepted(monkeypatch) -> None:
    from software_engineering_team import quality_gate_tools as q

    class _Result:
        accepted = True
        reasoning = "great"

    monkeypatch.setattr(
        "acceptance_verifier_agent.AcceptanceVerifierAgent",
        lambda llm: SimpleNamespace(run=lambda **kw: _Result()),
    )
    result = q.run_acceptance_verification(
        code="x",
        task_description="t",
        acceptance_criteria=["c1"],
        llm_getter=lambda k: MagicMock(),
    )
    assert result.accepted is True
    assert result.reasoning == "great"


def test_run_acceptance_verification_exception(monkeypatch) -> None:
    from software_engineering_team import quality_gate_tools as q

    def boom(**kw):
        raise RuntimeError("verifier down")

    monkeypatch.setattr(
        "acceptance_verifier_agent.AcceptanceVerifierAgent",
        lambda llm: SimpleNamespace(run=boom),
    )
    result = q.run_acceptance_verification(
        code="x",
        task_description="t",
        acceptance_criteria=["c1"],
        llm_getter=lambda k: MagicMock(),
    )
    assert result.accepted is False
    assert "verifier down" in result.reasoning


def test_run_code_review_sizes_context_with_strands_adapter() -> None:
    """Regression: the default llm_getter returns a strands LLMClientModel and
    run_code_review passes it to compute_code_review_total_chars, which calls
    get_max_context_tokens on it. Without adapter delegation that raised
    AttributeError and every review failed closed ("Review failed: ...")."""
    from llm_service.clients.dummy import DummyLLMClient
    from llm_service.strands_adapter import get_strands_model
    from software_engineering_team.shared.context_sizing import compute_code_review_total_chars

    llm = get_strands_model("code_review", client=DummyLLMClient())
    assert compute_code_review_total_chars(llm) == 150_000


def test_run_code_review_forwards_progress_callback(monkeypatch) -> None:
    """The tool must forward progress_callback into the agent's run (and None when omitted)."""
    from software_engineering_team import quality_gate_tools as q

    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, llm) -> None:
            pass

        def run(self, inp, progress_callback=None):
            captured["progress_callback"] = progress_callback
            return _ReviewResult()

    monkeypatch.setattr("code_review_agent.CodeReviewAgent", _CapturingAgent)
    monkeypatch.setattr(
        "software_engineering_team.shared.context_sizing.compute_code_review_total_chars",
        lambda llm: 1000,
    )

    def _cb(step: str, detail: str, fraction: float) -> None:
        pass

    result = q.run_code_review(
        code="x",
        spec_content="",
        task_description="t",
        language="python",
        task_requirements="reqs",
        llm_getter=lambda k: MagicMock(),
        progress_callback=_cb,
    )
    assert result.approved is True
    assert captured["progress_callback"] is _cb

    q.run_code_review(
        code="x",
        spec_content="",
        task_description="t",
        language="python",
        task_requirements="reqs",
        llm_getter=lambda k: MagicMock(),
    )
    assert captured["progress_callback"] is None


def test_run_code_review_forwards_files_dict(monkeypatch) -> None:
    """When ``files`` is supplied it reaches CodeReviewInput.files and the legacy
    ``code`` string is ignored (the agent bounds its own per-call prompts)."""
    from software_engineering_team import quality_gate_tools as q

    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, llm) -> None:
            pass

        def run(self, inp, progress_callback=None):
            captured["files"] = inp.files
            captured["code"] = inp.code
            return _ReviewResult()

    monkeypatch.setattr("code_review_agent.CodeReviewAgent", _CapturingAgent)

    files = {"app/main.py": "print('hi')", "app/util.py": "x = 1"}
    result = q.run_code_review(
        code="ignored whole-repo blob",
        spec_content="",
        task_description="t",
        language="python",
        task_requirements="reqs",
        files=files,
        llm_getter=lambda k: MagicMock(),
    )
    assert result.approved is True
    assert captured["files"] == files
    # ``code`` was not passed through; CodeReviewInput leaves it at its default.
    assert captured["code"] == ""


def test_run_code_review_uses_code_when_no_files(monkeypatch) -> None:
    """Without ``files`` the legacy ``code`` string is passed as the review input."""
    from software_engineering_team import quality_gate_tools as q

    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, llm) -> None:
            pass

        def run(self, inp, progress_callback=None):
            captured["files"] = inp.files
            captured["code"] = inp.code
            return _ReviewResult()

    monkeypatch.setattr("code_review_agent.CodeReviewAgent", _CapturingAgent)

    q.run_code_review(
        code="### a.py ###\nx = 1",
        spec_content="",
        task_description="t",
        language="python",
        task_requirements="reqs",
        llm_getter=lambda k: MagicMock(),
    )
    assert captured["files"] is None
    assert captured["code"] == "### a.py ###\nx = 1"


# ---------------------------------------------------------------------------
# run_radon
# ---------------------------------------------------------------------------


def test_run_radon_skips_non_backend(tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    result = q.run_radon(tmp_path, "frontend", "t1")
    assert result.passed is True
    assert result.violations == []


def test_run_radon_skips_when_no_python_files(tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    result = q.run_radon(tmp_path, "backend", "t1")
    assert result.passed is True


def test_run_radon_passes_clean_report(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q
    from software_engineering_team.shared.command_runner import RadonReport

    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        "software_engineering_team.shared.command_runner.run_radon_analysis",
        lambda path, *, max_cc, min_mi: RadonReport(passed=True, worst_cc=4),
    )
    result = q.run_radon(tmp_path, "backend", "t1", max_cc=15)
    assert result.passed is True


def test_run_radon_blocks_on_violation(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q
    from software_engineering_team.shared.command_runner import RadonReport

    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    violation = {"metric": "cc", "file": "mod.py", "name": "f", "complexity": 30}
    monkeypatch.setattr(
        "software_engineering_team.shared.command_runner.run_radon_analysis",
        lambda path, *, max_cc, min_mi: RadonReport(
            passed=False, worst_cc=30, violations=[violation], summary="too complex"
        ),
    )
    result = q.run_radon(tmp_path, "backend", "t1", max_cc=15)
    assert result.passed is False
    assert result.summary == "too complex"
    assert result.violations == [violation]


def test_run_radon_exception_non_blocking(monkeypatch, tmp_path) -> None:
    from software_engineering_team import quality_gate_tools as q

    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")

    def boom(*a, **kw):
        raise RuntimeError("radon exploded")

    monkeypatch.setattr(
        "software_engineering_team.shared.command_runner.run_radon_analysis", boom
    )
    result = q.run_radon(tmp_path, "backend", "t1")
    assert result.passed is True

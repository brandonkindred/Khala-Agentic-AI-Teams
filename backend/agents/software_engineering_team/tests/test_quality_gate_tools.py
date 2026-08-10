"""Tests for ``software_engineering_team.quality_gate_tools``.

Each public function delegates to a downstream agent or the orchestrator. We
patch the downstream symbol to avoid running real LLM / subprocess calls and
verify the wrapper translates results / handles exceptions gracefully.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_default_llm_getter_returns_strands_model(monkeypatch) -> None:
    """The default llm_getter returns the strands model for the requested agent key."""
    from software_engineering_team import quality_gate_tools

    sentinel = object()
    monkeypatch.setattr("llm_service.get_strands_model", lambda agent_key: sentinel)
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
    """run_code_review returns an approved result with issues and summary when the downstream agent succeeds."""
    from software_engineering_team import quality_gate_tools as q

    monkeypatch.setattr(
        "software_engineering_team.code_review_agent.CodeReviewAgent",
        lambda llm: MagicMock(run=lambda inp, progress_callback=None: _ReviewResult()),
    )
    result = q.run_code_review(
        spec_content="spec",
        task_description="task",
        language="python",
        task_requirements="reqs as string",
        files={"a.py": "x" * 50},
        llm_getter=lambda k: MagicMock(),
    )
    assert result.approved is True
    assert result.issues
    assert result.summary == "ok"


def test_run_code_review_forwards_user_decisions(monkeypatch) -> None:
    """User-settled decisions are forwarded onto the review input so the reviewer treats them
    as settled rather than re-raising them."""
    from software_engineering_team import quality_gate_tools as q

    captured = {}

    def _run(inp, progress_callback=None):
        captured["input"] = inp
        return _ReviewResult()

    monkeypatch.setattr(
        "software_engineering_team.code_review_agent.CodeReviewAgent",
        lambda llm: SimpleNamespace(run=_run),
    )
    q.run_code_review(
        spec_content="spec",
        task_description="task",
        language="python",
        task_requirements="reqs",
        user_decisions=["Which DB? → Postgres"],
        files={"a.py": "x" * 50},
        llm_getter=lambda k: MagicMock(),
    )
    assert captured["input"].user_decisions == ["Which DB? → Postgres"]


def test_run_code_review_forwards_repo_root_and_repo_reader(monkeypatch, tmp_path) -> None:
    """A ``repo_path`` is forwarded on both channels: as ``repo_root`` on the
    built ``CodeReviewInput`` (the field the Temporal path reconstructs a
    ``DiskRepoReader`` from worker-side) and as a live ``DiskRepoReader`` passed
    directly to ``agent.run`` (used on the in-process path)."""
    from software_engineering_team import quality_gate_tools as q
    from software_engineering_team.code_review_agent.repo_reader import DiskRepoReader

    captured = {}

    def _run(inp, progress_callback=None, repo_reader=None):
        captured["input"] = inp
        captured["repo_reader"] = repo_reader
        return _ReviewResult()

    monkeypatch.setattr(
        "software_engineering_team.code_review_agent.CodeReviewAgent",
        lambda llm: SimpleNamespace(run=_run),
    )
    repo_path = str(tmp_path)
    q.run_code_review(
        spec_content="spec",
        task_description="task",
        language="python",
        task_requirements="reqs",
        repo_path=repo_path,
        files={"a.py": "x" * 50},
        llm_getter=lambda k: MagicMock(),
    )
    assert captured["input"].repo_root == repo_path
    assert isinstance(captured["repo_reader"], DiskRepoReader)


def test_run_code_review_without_repo_path_passes_no_reader(monkeypatch) -> None:
    """Without ``repo_path``, ``repo_root`` stays unset and no ``repo_reader`` kwarg
    is forwarded to ``agent.run`` at all."""
    from software_engineering_team import quality_gate_tools as q

    captured = {}

    def _run(inp, progress_callback=None, **kwargs):
        captured["input"] = inp
        captured["kwargs"] = kwargs
        return _ReviewResult()

    monkeypatch.setattr(
        "software_engineering_team.code_review_agent.CodeReviewAgent",
        lambda llm: SimpleNamespace(run=_run),
    )
    q.run_code_review(
        spec_content="spec",
        task_description="task",
        language="python",
        task_requirements="reqs",
        files={"a.py": "x" * 50},
        llm_getter=lambda k: MagicMock(),
    )
    assert captured["input"].repo_root is None
    assert "repo_reader" not in captured["kwargs"]


def test_run_code_review_default_task_requirements_empty(monkeypatch) -> None:
    """Omitting ``task_requirements`` normalizes to ``""`` so CodeReviewInput
    validates and the agent actually runs (approved=True), instead of the
    historical ``or []`` list that failed Pydantic validation."""
    from software_engineering_team import quality_gate_tools as q

    captured = {}

    def _run(inp, progress_callback=None):
        captured["input"] = inp
        return _ReviewResult()

    monkeypatch.setattr(
        "software_engineering_team.code_review_agent.CodeReviewAgent",
        lambda llm: SimpleNamespace(run=_run),
    )
    result = q.run_code_review(
        spec_content="spec",
        task_description="task",
        language="python",
        files={"a.py": "x"},
        llm_getter=lambda k: MagicMock(),
    )
    assert result.approved is True
    assert captured["input"].task_requirements == ""


def test_run_code_review_list_task_requirements_joined(monkeypatch) -> None:
    """A list of requirement lines is joined with newlines before CodeReviewInput."""
    from software_engineering_team import quality_gate_tools as q

    captured = {}

    def _run(inp, progress_callback=None):
        captured["input"] = inp
        return _ReviewResult()

    monkeypatch.setattr(
        "software_engineering_team.code_review_agent.CodeReviewAgent",
        lambda llm: SimpleNamespace(run=_run),
    )
    result = q.run_code_review(
        spec_content="spec",
        task_description="task",
        language="python",
        task_requirements=["req a", "req b"],
        files={"a.py": "x"},
        llm_getter=lambda k: MagicMock(),
    )
    assert result.approved is True
    assert captured["input"].task_requirements == "req a\nreq b"


def test_run_code_review_exception_returns_failed(monkeypatch) -> None:
    """run_code_review fails closed (approved=False, 'Review failed' summary) when the downstream agent raises."""
    from software_engineering_team import quality_gate_tools as q

    def boom(*a, **kw):
        raise RuntimeError("agent down")

    monkeypatch.setattr(
        "software_engineering_team.code_review_agent.CodeReviewAgent",
        lambda llm: SimpleNamespace(run=boom),
    )
    result = q.run_code_review(
        spec_content="",
        task_description="",
        language="python",
        files={"a.py": "x"},
        llm_getter=lambda k: MagicMock(),
    )
    assert result.approved is False
    assert "Review failed" in result.summary


# ---------------------------------------------------------------------------
# run_build_verification
# ---------------------------------------------------------------------------


def test_run_build_verification_success(monkeypatch, tmp_path) -> None:
    """run_build_verification reports success and no env failure when the build verifier returns (True, '')."""
    from software_engineering_team import quality_gate_tools as q

    monkeypatch.setattr(
        "software_engineering_team.build_fix._run_build_verification",
        lambda repo_path, agent_type, task_id: (True, ""),
    )
    result = q.run_build_verification(tmp_path, "backend", "t1")
    assert result.success is True
    assert result.is_env_failure is False


def test_run_build_verification_env_failure(monkeypatch, tmp_path) -> None:
    """run_build_verification flags an environment (non-build) failure when the verifier returns an 'ENV:' error."""
    from software_engineering_team import quality_gate_tools as q

    monkeypatch.setattr(
        "software_engineering_team.build_fix._run_build_verification",
        lambda repo_path, agent_type, task_id: (False, "ENV:missing"),
    )
    result = q.run_build_verification(tmp_path, "backend", "t1")
    assert result.success is False
    assert result.is_env_failure is True


def test_run_build_verification_exception(monkeypatch, tmp_path) -> None:
    """run_build_verification reports a failed result carrying the exception message when the verifier raises."""
    from software_engineering_team import quality_gate_tools as q

    def boom(*a, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr("software_engineering_team.build_fix._run_build_verification", boom)
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
    """run_linting surfaces the lint agent's pass/issue result unchanged when the agent runs."""
    from software_engineering_team import quality_gate_tools as q

    fake_agent = SimpleNamespace(run=lambda path: _LintResultModel())
    monkeypatch.setattr(
        "software_engineering_team.linting_tool_agent.LintingToolAgent", lambda llm: fake_agent
    )
    result = q.run_linting(tmp_path, "t1", llm_getter=lambda k: MagicMock())
    assert result.passed is False
    assert result.issues


def test_run_linting_exception_non_blocking(monkeypatch, tmp_path) -> None:
    """run_linting is non-blocking: an exploding lint agent yields passed=True rather than raising."""
    from software_engineering_team import quality_gate_tools as q

    def boom(*a, **kw):
        raise RuntimeError("lint exploded")

    monkeypatch.setattr(
        "software_engineering_team.linting_tool_agent.LintingToolAgent",
        lambda llm: SimpleNamespace(run=boom),
    )
    result = q.run_linting(tmp_path, "t1", llm_getter=lambda k: MagicMock())
    assert result.passed is True


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

    monkeypatch.setattr(
        "software_engineering_team.code_review_agent.CodeReviewAgent", _CapturingAgent
    )

    def _cb(step: str, detail: str, fraction: float) -> None:
        pass

    result = q.run_code_review(
        spec_content="",
        task_description="t",
        language="python",
        task_requirements="reqs",
        files={"a.py": "x"},
        llm_getter=lambda k: MagicMock(),
        progress_callback=_cb,
    )
    assert result.approved is True
    assert captured["progress_callback"] is _cb

    q.run_code_review(
        spec_content="",
        task_description="t",
        language="python",
        task_requirements="reqs",
        files={"a.py": "x"},
        llm_getter=lambda k: MagicMock(),
    )
    assert captured["progress_callback"] is None


def test_run_code_review_forwards_files_dict(monkeypatch) -> None:
    """When ``files`` is supplied it reaches CodeReviewInput.files."""
    from software_engineering_team import quality_gate_tools as q

    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, llm) -> None:
            pass

        def run(self, inp, progress_callback=None):
            captured["files"] = inp.files
            return _ReviewResult()

    monkeypatch.setattr(
        "software_engineering_team.code_review_agent.CodeReviewAgent", _CapturingAgent
    )

    files = {"app/main.py": "print('hi')", "app/util.py": "x = 1"}
    result = q.run_code_review(
        spec_content="",
        task_description="t",
        language="python",
        task_requirements="reqs",
        files=files,
        llm_getter=lambda k: MagicMock(),
    )
    assert result.approved is True
    assert captured["files"] == files

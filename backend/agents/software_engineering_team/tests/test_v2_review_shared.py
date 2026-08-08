"""Branch coverage for the shared code-v2 review body (``shared.v2_review``).

The per-team review tests (``test_v2_review_phase`` / ``test_v2_fe_review_phase``)
exercise the shared ``run_review`` / ``run_microtask_review`` through the per-team
delegates and pin the externally observable behaviour. They do not, however,
drive every branch of the shared body — the microtask path's lint / code-review /
tool-agent / build-fail / detail-callback branches and the ``run_review`` lint
exception path are not reachable through those tests' agent combos. This file
calls the shared functions directly with a synthetic :class:`ReviewConfig` and
stub runners so each branch is exercised on its own, independent of the
per-team ``Agent`` patch surface.

Preconditions:
    - The synthetic ``ReviewConfig`` uses a ``tool_phase_input_factory`` that
      accepts arbitrary kwargs, so both the context and no-context variants are
      callable without binding a per-team ``ToolAgentPhaseInput``.
    - ``llm`` is a ``DummyLLMClient`` so ``_review_steps_run_sequentially`` forces
      the sequential branch: these tests drive the shared body's branch logic
      deterministically (no worker threads), not the concurrent fan-out, which is
      pinned by the per-team ``test_*_run_review_steps_run_concurrently`` barrier
      tests.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional, Tuple
from unittest.mock import MagicMock

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.shared.models import ReviewContext, SystemArchitecture
from software_engineering_team.shared.v2_models import ReviewIssue
from software_engineering_team.shared.v2_review import (
    ReviewConfig,
    _lint_passed,
    _maybe_build_change_surface_from_pairs,
    run_microtask_review,
    run_review,
)


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        id="t1",
        title="T",
        description="desc",
        requirements="reqs",
        acceptance_criteria=["AC"],
    )


def _microtask() -> SimpleNamespace:
    return SimpleNamespace(id="mt-1", title="MT", description="mdesc")


def _execution_result(files: Dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(files=files)


def _build_config(
    *,
    lint_severity_remap: Optional[Dict[str, str]] = None,
    tool_rec_source_prefix: Optional[str] = None,
    tool_rec_recommendation_uses_rec: bool = False,
    tool_phase_includes_context: bool = False,
    passed_includes_lint_review: bool = True,
    log_review_summary: bool = False,
) -> ReviewConfig:
    """A synthetic config with a permissive tool-phase-input factory.

    Postconditions: returns a frozen ``ReviewConfig`` whose
    ``tool_phase_input_factory`` accepts any kwargs (returns a SimpleNamespace),
    so both context and no-context variants are callable without a per-team
    ``ToolAgentPhaseInput``.
    """
    return ReviewConfig(
        lint_agent_type="backend",
        build_fail_recommendation_review="fix it",
        lint_severity_remap=lint_severity_remap,
        tool_rec_source_prefix=tool_rec_source_prefix,
        tool_rec_recommendation_uses_rec=tool_rec_recommendation_uses_rec,
        tool_phase_includes_context=tool_phase_includes_context,
        passed_includes_lint_review=passed_includes_lint_review,
        log_review_summary=log_review_summary,
        tool_phase_input_factory=lambda **kw: SimpleNamespace(**kw),
        summary_review=lambda passed, build_ok, lint_ok, n, c: (
            f"r:{passed},{build_ok},{lint_ok},{n},{c}"
        ),
        summary_microtask=lambda mid, passed, build_ok, lint_ok, n, c: (
            f"m:{mid},{passed},{build_ok},{lint_ok},{n},{c}"
        ),
        microtask_intro=lambda mid, n: f"intro:{mid}:{n}",
    )


def _noop_runners() -> Dict[str, Any]:
    """Stub runners that return no issues and never touch an LLM/agent."""
    return {
        "llm_review_fn": lambda *, llm, task, files, **kw: [],
        "qa_agent_fn": lambda *, qa_agent, files, language, task_description, task_id, context="", cache=None: [],
        "security_agent_fn": lambda *, security_agent, files, language, task_description, task_id, context="", cache=None: [],
        "build_verify_fn": _build_verify_fn,
    }


def _build_verify_fn(
    repo_path: Path, build_verifier: Optional[Callable], task_id: str
) -> Tuple[bool, str]:
    """Mirror of the per-team ``_run_build_verification`` for the stub config."""
    if build_verifier is None:
        return True, "No build verifier provided; skipping."
    try:
        return build_verifier(repo_path, "backend_code_v2", task_id)
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# run_review branches
# ---------------------------------------------------------------------------


def test_run_review_lint_agent_raises_is_logged_not_raised(tmp_path: Path, caplog) -> None:
    """A raising linting tool agent is logged and skipped (run_review lint except)."""
    import logging

    caplog.set_level(logging.WARNING, logger="software_engineering_team.shared.v2_review")
    config = _build_config()

    def _boom(*a, **kw):
        raise RuntimeError("lint crashed")

    result = run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        linting_tool_agent=MagicMock(run=_boom),
        language="python",
        **_noop_runners(),
    )
    assert result.passed  # lint failure was swallowed; no blocking issue
    assert any("lint crashed" in r.message for r in caplog.records)


def test_run_review_forwards_language_to_llm_review_fn(tmp_path: Path) -> None:
    """The code-review step's LLM fallback must see the caller's ``language``
    (not silently drop it) -- a fallback that forwards it to ``CodeReviewInput``
    (e.g. backend's coordinator-backed fallback) would otherwise review the
    code under ``CodeReviewInput``'s ``typescript`` default regardless of the
    caller's actual language."""
    captured: dict = {}

    def _spy_llm_review_fn(*, llm, task, files, **kw):
        captured["language"] = kw.get("language")
        return []

    runners = _noop_runners()
    runners["llm_review_fn"] = _spy_llm_review_fn

    run_review(
        config=_build_config(),
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        language="python",
        **runners,
    )
    assert captured["language"] == "python"


def test_lint_passed_defends_missing_execution_result() -> None:
    """A lint-tool result lacking ``execution_result`` entirely (not just a
    falsy inner ``.success``) must not raise -- only the innermost lookup was
    previously getattr-guarded, so ``lint_result.execution_result`` itself
    could raise AttributeError for a differently-shaped lint tool return."""
    assert _lint_passed(SimpleNamespace()) is True  # nothing to report -> assume success
    assert _lint_passed(SimpleNamespace(passed=False)) is False
    assert _lint_passed(SimpleNamespace(execution_result=SimpleNamespace(success=False))) is False
    assert _lint_passed(SimpleNamespace(execution_result=SimpleNamespace(success=True))) is True
    # execution_result present but success looked up via getattr default too.
    assert _lint_passed(SimpleNamespace(execution_result=SimpleNamespace())) is True


def test_run_review_lint_fail_with_remap_blocks(tmp_path: Path) -> None:
    """A failing lint with a severity remap maps 'error' -> 'high' (blocking)."""
    config = _build_config(
        lint_severity_remap={"error": "high", "warning": "medium", "info": "low"}
    )

    class _LintIssue:
        severity = "error"
        message = "syntax"
        file_path = "x.py"

    lint_agent = MagicMock()
    lint_agent.run.return_value = MagicMock(
        execution_result=MagicMock(success=False), passed=False, linter_issues=[_LintIssue()]
    )

    result = run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        linting_tool_agent=lint_agent,
        language="python",
        **_noop_runners(),
    )
    assert result.lint_ok is False
    lint_issues = [i for i in result.issues if i.source == "lint"]
    assert lint_issues and lint_issues[0].severity == "high"  # remap applied


def test_run_review_log_summary_branch(tmp_path: Path, caplog) -> None:
    """``log_review_summary=True`` emits the INFO summary line."""
    import logging

    caplog.set_level(logging.INFO, logger="software_engineering_team.shared.v2_review")
    config = _build_config(log_review_summary=True)

    run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        language="python",
        **_noop_runners(),
    )
    assert any("passed=" in r.message for r in caplog.records)


def test_run_review_tool_agents_recommendations_and_raise(tmp_path: Path) -> None:
    """Tool agents contribute issues + recommendation issues; a raising agent is skipped."""
    from software_engineering_team.backend_code_v2_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )

    config = _build_config(
        tool_rec_source_prefix="tool_",
        tool_rec_recommendation_uses_rec=True,
        tool_phase_includes_context=True,
    )

    good = MagicMock()
    good.review.return_value = ToolAgentPhaseOutput(
        issues=[
            ReviewIssue(
                source="tool_qa", severity="low", description="from tool", recommendation="ok"
            )
        ],
        recommendations=["add tests"],
    )
    raising = MagicMock()
    raising.review.side_effect = RuntimeError("boom")
    bare = object()  # no .review method

    result = run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        tool_agents={
            ToolAgentKind.TESTING_QA: good,
            ToolAgentKind.SECURITY: raising,
            ToolAgentKind.GENERAL: bare,
        },
        language="python",
        **_noop_runners(),
    )
    # recommendation became a tool_-prefixed info issue carrying the rec.
    rec = [i for i in result.issues if i.description == "add tests"]
    assert rec and rec[0].source == "tool_testing_qa" and rec[0].recommendation == "add tests"
    assert any(i.description == "from tool" for i in result.issues)  # out.issues folded in


def test_run_review_threads_repo_path_into_tool_agents(tmp_path: Path) -> None:
    """Full-review tool agents receive the checkout's repo_path, not the empty default.

    Regression guard: the collapsed ``run_review`` must pass ``tool_repo_path`` so
    ``ToolAgentPhaseInput.repo_path`` matches the contract ``run_microtask_review``
    (and the pre-collapse backend/frontend implementations) gave full-review tool
    agents — losing it silently drops repository context in the full Review phase.
    """
    from software_engineering_team.backend_code_v2_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )

    config = _build_config()
    captured: dict = {}
    good = MagicMock()
    good.review.side_effect = lambda phase_inp: (
        captured.update(repo_path=phase_inp.repo_path)
        or ToolAgentPhaseOutput(issues=[], recommendations=[])
    )

    run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        tool_agents={ToolAgentKind.TESTING_QA: good},
        language="python",
        **_noop_runners(),
    )
    assert captured["repo_path"] == str(tmp_path)


def test_run_review_raw_issue_count_from_llm_fallback(tmp_path: Path) -> None:
    """run_review forwards the LLM fallback's pre-grounding raw_issue_count onto
    ReviewResult via _code_review_step's _ReviewStepResult return value."""
    from software_engineering_team.shared.llm_review import LlmReviewOutput

    config = _build_config()
    result = run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: LlmReviewOutput(
            issues=[ReviewIssue(source="code_review", severity="low", description="kept")],
            raw_issue_count=3,
        ),
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
    )
    assert result.raw_issue_count == 3
    assert any(i.description == "kept" for i in result.issues)


def test_run_review_raw_issue_count_none_when_code_review_agent_succeeds(tmp_path: Path) -> None:
    """When the external code_review_agent succeeds, the LLM fallback never runs, so
    raw_issue_count stays None -- there is no raw/grounded distinction to report."""
    config = _build_config()

    class _Issue:
        severity = "medium"
        description = "from agent"
        file_path = "x.py"
        recommendation = ""

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[_Issue()])

    result = run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )
    assert result.raw_issue_count is None
    assert any(i.description == "from agent" for i in result.issues)


def test_run_review_code_review_agent_issue_uses_suggestion_field(tmp_path: Path) -> None:
    """The real CodeReviewIssue model carries fix guidance in ``suggestion``, not
    ``recommendation`` -- _code_review_step must read ``suggestion`` so that field isn't
    silently dropped from the external agent's issues."""
    config = _build_config()

    class _Issue:
        severity = "medium"
        description = "from agent"
        file_path = "x.py"
        suggestion = "do the fix"

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[_Issue()])

    result = run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )
    issue = next(i for i in result.issues if i.description == "from agent")
    assert issue.recommendation == "do the fix"


def test_run_review_code_review_agent_issue_falls_back_to_recommendation_field(
    tmp_path: Path,
) -> None:
    """Backward compatibility: an issue object with no ``suggestion`` attribute still
    populates ``recommendation`` from a legacy ``recommendation`` attribute."""
    config = _build_config()

    class _Issue:
        severity = "medium"
        description = "from agent"
        file_path = "x.py"
        recommendation = "legacy fix"

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[_Issue()])

    result = run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )
    issue = next(i for i in result.issues if i.description == "from agent")
    assert issue.recommendation == "legacy fix"


# ---------------------------------------------------------------------------
# run_microtask_review branches
# ---------------------------------------------------------------------------


def test_microtask_build_fail(tmp_path: Path) -> None:
    """A failing build verifier in the microtask path appends a critical build issue."""
    config = _build_config()
    result = run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        build_verifier=lambda *a, **k: (False, "compile error"),
        language="python",
        **_noop_runners(),
    )
    assert result.build_ok is False
    assert any(i.source == "build" and i.severity == "critical" for i in result.issues)


def test_microtask_lint_fail_and_raise_and_detail_callback(tmp_path: Path) -> None:
    """Microtask lint: detail_callback fires, a failing lint maps severity, a raising
    lint agent is logged-and-skipped."""
    config = _build_config(lint_severity_remap={"error": "high"})

    class _LintIssue:
        severity = "error"
        message = "syntax"
        file_path = "x.py"

    class _OutOfScopeLintIssue:
        # file_path not in the microtask's ``files`` -> filtered out by `continue`.
        severity = "error"
        message = "ignored"
        file_path = "other.py"

    lint_agent = MagicMock()
    lint_agent.run.return_value = MagicMock(
        execution_result=MagicMock(success=False),
        passed=False,
        linter_issues=[_LintIssue(), _OutOfScopeLintIssue()],
    )

    details: list[str] = []
    result = run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        linting_tool_agent=lint_agent,
        detail_callback=details.append,
        language="python",
        **_noop_runners(),
    )
    assert result.lint_ok is False
    lint_issues = [i for i in result.issues if i.source == "lint"]
    assert [i for i in lint_issues if i.severity == "high"]
    assert not any(i.description == "ignored" for i in lint_issues)  # out-of-scope filtered out
    assert "Running linter..." in details


def test_microtask_lint_agent_raises_is_swallowed(tmp_path: Path) -> None:
    """A raising microtask lint agent is logged and skipped (microtask lint except)."""
    config = _build_config()

    def _boom(*a, **kw):
        raise RuntimeError("lint crashed")

    result = run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        linting_tool_agent=MagicMock(run=_boom),
        language="python",
        **_noop_runners(),
    )
    assert result.lint_ok  # raise swallowed; lint stayed ok


def test_microtask_code_review_agent_path_and_raise(tmp_path: Path) -> None:
    """The microtask code-review-agent path folds its issues; a raise falls back to LLM."""
    config = _build_config()

    class _Issue:
        severity = "medium"
        description = "magic"
        file_path = "x.py"
        recommendation = "fix"

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[_Issue()])

    details: list[str] = []
    result = run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        code_review_agent=cr_agent,
        detail_callback=details.append,
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: [
            ReviewIssue(source="code_review", severity="low", description="llm")
        ],
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
    )
    assert any(i.source == "code_review" and i.description == "magic" for i in result.issues)
    assert "Running code review..." in details

    # Now the agent raises -> LLM fallback fires.
    cr_agent.run.side_effect = RuntimeError("crash")
    result2 = run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        code_review_agent=cr_agent,
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: [
            ReviewIssue(source="code_review", severity="low", description="llm")
        ],
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
    )
    assert any(i.description == "llm" for i in result2.issues)


def test_microtask_code_review_agent_issue_uses_suggestion_field(tmp_path: Path) -> None:
    """The microtask code-review-agent path reads ``suggestion`` (the real
    CodeReviewIssue field) into ``recommendation``, same as the full-task path."""
    config = _build_config()

    class _Issue:
        severity = "medium"
        description = "magic"
        file_path = "x.py"
        suggestion = "do the fix"

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[_Issue()])

    result = run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )
    issue = next(i for i in result.issues if i.description == "magic")
    assert issue.recommendation == "do the fix"


def test_microtask_raw_issue_count_from_llm_fallback(tmp_path: Path) -> None:
    """run_microtask_review forwards the LLM fallback's raw_issue_count too
    via _code_review_step's _ReviewStepResult return value."""
    from software_engineering_team.shared.llm_review import LlmReviewOutput

    config = _build_config()
    result = run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: LlmReviewOutput(
            issues=[ReviewIssue(source="code_review", severity="low", description="kept")],
            raw_issue_count=5,
        ),
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
    )
    assert result.raw_issue_count == 5


def test_code_review_agent_receives_architecture_and_spec_content(tmp_path: Path) -> None:
    """``architecture``/``spec_content`` passed to ``run_microtask_review`` reach the
    ``CodeReviewInput`` built for the external code-review agent, and default to
    ``None``/``""`` when omitted (backward compatible with existing callers)."""
    config = _build_config()
    architecture = SystemArchitecture(overview="layered architecture")

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        code_review_agent=cr_agent,
        language="python",
        review_context=ReviewContext(
            architecture=architecture, spec_content="the full project spec"
        ),
        llm_review_fn=lambda *, llm, task, files, **kw: [],
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
    )
    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.architecture is architecture
    assert cr_input.spec_content == "the full project spec"

    cr_agent.run.reset_mock()
    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        code_review_agent=cr_agent,
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: [],
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
    )
    cr_input_default = cr_agent.run.call_args.args[0]
    assert cr_input_default.architecture is None
    assert cr_input_default.spec_content == ""


def test_code_review_input_carries_repo_root_for_durable_reader(tmp_path: Path) -> None:
    """The ``CodeReviewInput`` built for the external agent carries ``repo_root`` set
    to the workspace path, so a durable Temporal review can rebuild the whole-repo
    reader worker-side (the live reader cannot cross that boundary)."""
    config = _build_config()

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        code_review_agent=cr_agent,
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: [],
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
    )
    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.repo_root == str(tmp_path)


def test_microtask_qa_and_security_with_detail_callback(tmp_path: Path) -> None:
    """The microtask QA/security branches emit their detail-callback messages."""
    config = _build_config()

    details: list[str] = []
    result = run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        qa_agent=MagicMock(),
        security_agent=MagicMock(),
        detail_callback=details.append,
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: [],
        qa_agent_fn=lambda *, qa_agent, files, language, task_description, task_id, context="", cache=None: [
            ReviewIssue(source="qa", severity="low", description="bug")
        ],
        security_agent_fn=lambda *, security_agent, files, language, task_description, task_id, context="", cache=None: [
            ReviewIssue(source="security", severity="low", description="vuln")
        ],
        build_verify_fn=_build_verify_fn,
    )
    assert "Running QA check..." in details
    assert "Running security scan..." in details
    assert any(i.source == "qa" for i in result.issues)
    assert any(i.source == "security" for i in result.issues)


def test_microtask_qa_and_security_hooks_without_cache_param_still_work(tmp_path: Path) -> None:
    """A qa_agent_fn/security_agent_fn predating the cache parameter (no
    ``cache`` in its signature, no ``**kwargs`` catch-all) must still work
    when the caller doesn't opt into caching (agent_review_cache omitted,
    defaulting to None) -- the cache kwarg must not be forced on every call."""
    config = _build_config()

    def _old_style_qa(*, qa_agent, files, language, task_description, task_id, context=""):
        return [ReviewIssue(source="qa", severity="low", description="bug")]

    def _old_style_security(
        *, security_agent, files, language, task_description, task_id, context=""
    ):
        return [ReviewIssue(source="security", severity="low", description="vuln")]

    result = run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        qa_agent=MagicMock(),
        security_agent=MagicMock(),
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: [],
        qa_agent_fn=_old_style_qa,
        security_agent_fn=_old_style_security,
        build_verify_fn=_build_verify_fn,
    )
    # A TypeError from the forced cache= kwarg would be swallowed and turned
    # into a synthetic "agent failed" issue instead -- assert the real
    # findings came through, not that.
    assert any(i.description == "bug" for i in result.issues)
    assert any(i.description == "vuln" for i in result.issues)


def test_microtask_tool_agents_no_context_variant(tmp_path: Path) -> None:
    """Microtask tool agents fold issues + recommendation issues with the no-context
    (frontend-style) config: ``source`` is ``kind.value`` verbatim and rec is blank."""
    from software_engineering_team.frontend_code_v2_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )

    config = _build_config(tool_rec_source_prefix=None, tool_rec_recommendation_uses_rec=False)

    tool_agent = MagicMock()
    tool_agent.review.return_value = ToolAgentPhaseOutput(
        issues=[ReviewIssue(source="security", severity="low", description="from tool")],
        recommendations=["add tests"],
    )

    result = run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"app.ts": "code"},
        tool_agents={ToolAgentKind.SECURITY: tool_agent},
        language="typescript",
        **_noop_runners(),
    )
    rec = [i for i in result.issues if i.description == "add tests"]
    assert (
        rec and rec[0].source == "security" and rec[0].recommendation == ""
    )  # no prefix, blank rec
    assert any(i.description == "from tool" for i in result.issues)


def test_microtask_tool_agent_raises_is_skipped(tmp_path: Path) -> None:
    """A raising microtask tool agent is logged and skipped (failure_context path)."""
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind

    config = _build_config()
    raising = MagicMock()
    raising.review.side_effect = RuntimeError("boom")

    result = run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"app.ts": "code"},
        tool_agents={ToolAgentKind.SECURITY: raising},
        language="typescript",
        **_noop_runners(),
    )
    assert result is not None  # did not raise


def test_microtask_tool_agent_cache_hit_skips_second_call(tmp_path: Path) -> None:
    """A second run_microtask_review call with byte-identical inputs and a
    shared tool_agent_cache reuses the first call's result -- the seam that
    closes the CR-gate-vs-QA/Security-gate residual 2x duplication."""
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.shared.agent_review import AgentReviewCache
    from software_engineering_team.shared.v2_models import ReviewIssue as _RI

    calls = {"n": 0}

    class _StubTestingQaAgent:
        def review(self, phase_inp):
            calls["n"] += 1
            return SimpleNamespace(
                issues=[_RI(source="testing_qa", severity="low", description="from tool")],
                recommendations=[],
            )

    config = _build_config()
    cache = AgentReviewCache()
    common = dict(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"app.ts": "code"},
        tool_agents={ToolAgentKind.TESTING_QA: _StubTestingQaAgent()},
        language="typescript",
        tool_agent_cache=cache,
        **_noop_runners(),
    )

    first = run_microtask_review(**common)
    second = run_microtask_review(**common)

    assert calls["n"] == 1  # second call was served entirely from cache
    assert any(i.description == "from tool" for i in first.issues)
    assert any(i.description == "from tool" for i in second.issues)


def test_microtask_tool_agent_cache_misses_on_changed_files(tmp_path: Path) -> None:
    """Different ``files`` (the ``current_files`` the cache key is hashed
    from) between two calls busts the cache -- a batch-fix between the CR
    gate's call and the QA gate's call must recompute for real."""
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.shared.agent_review import AgentReviewCache
    from software_engineering_team.shared.v2_models import ReviewIssue as _RI

    calls = {"n": 0}

    class _StubSecurityAgent:
        def review(self, phase_inp):
            calls["n"] += 1
            return SimpleNamespace(
                issues=[_RI(source="security", severity="low", description="from tool")],
                recommendations=[],
            )

    config = _build_config()
    cache = AgentReviewCache()
    common = dict(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        tool_agents={ToolAgentKind.SECURITY: _StubSecurityAgent()},
        language="typescript",
        tool_agent_cache=cache,
        **_noop_runners(),
    )

    run_microtask_review(files={"app.ts": "code v1"}, **common)
    run_microtask_review(files={"app.ts": "code v2"}, **common)

    assert calls["n"] == 2  # changed content -> both calls hit the agent


def test_microtask_tool_agent_cache_none_default_is_unchanged_passthrough(tmp_path: Path) -> None:
    """Omitting tool_agent_cache (the default) preserves today's unconditional-call
    behavior -- every existing caller that doesn't opt in is unaffected."""
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.shared.v2_models import ReviewIssue as _RI

    calls = {"n": 0}

    class _StubAgent:
        def review(self, phase_inp):
            calls["n"] += 1
            return SimpleNamespace(
                issues=[_RI(source="security", severity="low", description="from tool")],
                recommendations=[],
            )

    config = _build_config()
    common = dict(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"app.ts": "code"},
        tool_agents={ToolAgentKind.SECURITY: _StubAgent()},
        language="typescript",
        **_noop_runners(),
    )

    run_microtask_review(**common)
    run_microtask_review(**common)

    assert calls["n"] == 2  # no cache given -> both calls are live, as before


def test_microtask_tool_agent_malformed_output_is_never_cached_and_stays_contained(
    tmp_path: Path,
) -> None:
    """A tool agent returning a malformed (unfoldable) output must not raise on
    the first call or the retry -- and must not be cached (so the second call
    is a live retry, not a cache-hit), meaning every call retries live instead
    of replaying the same failure forever."""
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.shared.agent_review import AgentReviewCache

    calls = {"n": 0}

    class _MalformedAgent:
        def review(self, phase_inp):
            calls["n"] += 1
            return None  # no .issues/.recommendations -> AttributeError when folded

    config = _build_config()
    cache = AgentReviewCache()
    common = dict(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"app.ts": "code"},
        tool_agents={ToolAgentKind.SECURITY: _MalformedAgent()},
        language="typescript",
        tool_agent_cache=cache,
        **_noop_runners(),
    )

    first = run_microtask_review(**common)
    second = run_microtask_review(**common)

    assert first is not None and second is not None  # neither call raised
    assert calls["n"] == 2  # the bad output was never cached -> both calls were live


def test_microtask_intro_logged(tmp_path: Path, caplog) -> None:
    """The microtask opening INFO line uses the config's ``microtask_intro``."""
    import logging

    caplog.set_level(logging.INFO, logger="software_engineering_team.shared.v2_review")
    config = _build_config()

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        language="python",
        **_noop_runners(),
    )
    assert any("intro:mt-1:1" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _maybe_build_change_surface_from_pairs
# ---------------------------------------------------------------------------


def test_maybe_build_change_surface_empty_new_contents_skips_builder(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        "software_engineering_team.shared.v2_review.build_change_surface_from_pairs",
        lambda *a, **kw: calls.append((a, kw)),
    )

    result = _maybe_build_change_surface_from_pairs({}, old_contents={"a.py": "x"})

    assert result is None
    assert calls == []


def test_maybe_build_change_surface_identical_maps_skip_builder(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        "software_engineering_team.shared.v2_review.build_change_surface_from_pairs",
        lambda *a, **kw: calls.append((a, kw)),
    )
    same = {"a.py": "unchanged\n"}

    result = _maybe_build_change_surface_from_pairs(same, old_contents=dict(same))

    assert result is None
    assert calls == []


def test_maybe_build_change_surface_meaningful_diff_calls_builder_once(monkeypatch) -> None:
    calls: list = []
    old = {"a.py": "def f():\n    return 0\n"}
    new = {"a.py": "def f():\n    return 1\n"}
    real_builder = _real_build_change_surface_from_pairs()

    def _spy(new_contents, old_contents=None):
        calls.append((new_contents, old_contents))
        return real_builder(new_contents, old_contents)

    monkeypatch.setattr(
        "software_engineering_team.shared.v2_review.build_change_surface_from_pairs", _spy
    )

    result = _maybe_build_change_surface_from_pairs(new, old_contents=old)

    assert len(calls) == 1
    assert calls[0] == (new, old)
    assert result is not None
    assert not result.is_empty
    assert "a.py" in result.blocks


def test_maybe_build_change_surface_none_old_contents_treated_as_new_file() -> None:
    new = {"a.py": "def f():\n    return 1\n"}

    result = _maybe_build_change_surface_from_pairs(new, old_contents=None)

    assert result is not None
    assert not result.is_empty
    assert "### a.py ###" in result.code


def test_maybe_build_change_surface_empty_result_is_none_not_fake_surface() -> None:
    # Distinct key sets whose only overlapping path is identical -> the
    # builder still has to run (dicts are not equal), but nothing is
    # meaningfully different, so no surface should be returned.
    new = {"a.py": "same\n"}
    old = {"a.py": "same\n", "unrelated.py": "irrelevant\n"}

    result = _maybe_build_change_surface_from_pairs(new, old_contents=old)

    assert result is None


def _real_build_change_surface_from_pairs():
    from software_engineering_team.code_review_agent.change_surface import (
        build_change_surface_from_pairs,
    )

    return build_change_surface_from_pairs

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

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional, Tuple
from unittest.mock import MagicMock

from llm_service.clients.dummy import DummyLLMClient
from shared.dev_models.models import ReviewContext, SystemArchitecture
from software_engineering_team.code_review_agent.repo_reader import DiskRepoReader
from software_engineering_team.shared.v2_models import ReviewIssue
from software_engineering_team.shared.v2_review import (
    ReviewConfig,
    _lint_passed,
    _maybe_build_change_surface_from_pairs,
    _patch_has_any_removal,
    _resolve_change_surface_for_review,
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


def test_run_review_forwards_review_context_to_llm_review_fn(tmp_path: Path) -> None:
    """The code-review step's LLM fallback must see the caller's ``review_context``
    object unchanged -- surface-first wiring (diff-first review) must not drop the
    architecture/spec context the fallback reasons over."""
    captured: dict = {}

    def _spy_llm_review_fn(*, llm, task, files, **kw):
        captured["review_context"] = kw.get("review_context")
        return []

    runners = _noop_runners()
    runners["llm_review_fn"] = _spy_llm_review_fn
    ctx = ReviewContext(architecture=SystemArchitecture(overview="layered"), spec_content="spec")

    run_review(
        config=_build_config(),
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        language="python",
        review_context=ctx,
        **runners,
    )
    assert captured["review_context"] is ctx


def test_run_review_forwards_grounding_flag_to_llm_review_fn(tmp_path: Path) -> None:
    """``enable_llm_review_grounding`` must reach the LLM fallback unchanged, both at
    its default (True) and when a caller explicitly disables it -- this is the kill
    switch for ungrounded-claim filtering and must never be silently overridden."""
    captured: dict = {}

    def _spy_llm_review_fn(*, llm, task, files, **kw):
        captured["enable_llm_review_grounding"] = kw.get("enable_llm_review_grounding")
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
    assert captured["enable_llm_review_grounding"] is True

    run_review(
        config=_build_config(),
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        language="python",
        enable_llm_review_grounding=False,
        **runners,
    )
    assert captured["enable_llm_review_grounding"] is False


def test_code_review_agent_receives_disk_repo_reader(tmp_path: Path, monkeypatch) -> None:
    """The ``DiskRepoReader`` built from ``repo_path`` must reach the external
    ``code_review_agent.run(...)`` call as ``repo_reader=`` -- surface-first wiring must
    keep attaching it so later passes can resolve callers outside the change surface."""
    sentinel_reader = object()
    monkeypatch.setattr(
        "software_engineering_team.shared.v2_review.build_disk_repo_reader",
        lambda repo_path: sentinel_reader,
    )

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_review(
        config=_build_config(),
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )
    assert cr_agent.run.call_args.kwargs["repo_reader"] is sentinel_reader


def test_microtask_forwards_language_review_context_and_grounding_to_llm_review_fn(
    tmp_path: Path,
) -> None:
    """``run_microtask_review``'s LLM fallback must see ``language``, ``review_context``,
    and ``enable_llm_review_grounding`` unchanged too -- these three are only pinned for
    ``run_review`` elsewhere in this file; the microtask path shares ``_code_review_step``
    but had no equivalent coverage."""
    captured: dict = {}

    def _spy_llm_review_fn(*, llm, task, files, **kw):
        captured["language"] = kw.get("language")
        captured["review_context"] = kw.get("review_context")
        captured["enable_llm_review_grounding"] = kw.get("enable_llm_review_grounding")
        return []

    ctx = ReviewContext(architecture=SystemArchitecture(overview="layered"), spec_content="spec")

    run_microtask_review(
        config=_build_config(),
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files={"x.py": "code"},
        language="python",
        review_context=ctx,
        enable_llm_review_grounding=False,
        llm_review_fn=_spy_llm_review_fn,
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
    )
    assert captured["language"] == "python"
    assert captured["review_context"] is ctx
    assert captured["enable_llm_review_grounding"] is False


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


def test_run_review_shared_review_context_built_once_and_reused_across_tool_agents(
    tmp_path: Path,
) -> None:
    """Every wired tool agent in one review pass must receive the identical
    shared_review_context object -- the once-per-microtask cache-marked
    segment, not a fresh copy rebuilt per agent."""
    from llm_service import CacheBreakpoint
    from software_engineering_team.backend_code_v2_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )

    config = _build_config()
    captured: list = []

    def _capture(kind_name):
        agent = MagicMock()
        agent.review.side_effect = lambda phase_inp: (
            captured.append((kind_name, phase_inp.shared_review_context))
            or ToolAgentPhaseOutput(issues=[], recommendations=[])
        )
        return agent

    run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        tool_agents={
            ToolAgentKind.TESTING_QA: _capture("qa"),
            ToolAgentKind.SECURITY: _capture("security"),
        },
        language="python",
        **_noop_runners(),
    )

    assert len(captured) == 2
    contexts = [ctx for _, ctx in captured]
    assert contexts[0] is contexts[1]
    assert len(contexts[0]) == 1
    assert isinstance(contexts[0][0], CacheBreakpoint)
    # Only the (internal, non-repository-controlled) task description is
    # cache-marked; the reviewed code must never appear here -- see
    # build_shared_tool_agent_review_system_content's docstring.
    assert contexts[0][0].text == "**Task:** desc"


def test_run_review_shared_review_context_none_when_task_description_blank(
    tmp_path: Path,
) -> None:
    """No task description -> shared_review_context is None, not an empty list."""
    from software_engineering_team.backend_code_v2_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )

    config = _build_config()
    captured: list = []
    good = MagicMock()
    good.review.side_effect = lambda phase_inp: (
        captured.append(phase_inp.shared_review_context)
        or ToolAgentPhaseOutput(issues=[], recommendations=[])
    )

    blank_task = SimpleNamespace(
        id="t1", title="T", description="", requirements="reqs", acceptance_criteria=["AC"]
    )
    run_review(
        config=config,
        llm=DummyLLMClient(),
        task=blank_task,
        execution_result=_execution_result({"x.py": "code"}),
        repo_path=tmp_path,
        tool_agents={ToolAgentKind.TESTING_QA: good},
        language="python",
        **_noop_runners(),
    )

    assert captured == [None]


def test_run_review_raw_issue_count_from_llm_fallback(tmp_path: Path) -> None:
    """run_review forwards the LLM fallback's pre-grounding raw_issue_count onto
    ReviewResult via _code_review_step's _ReviewStepResult return value."""
    from software_engineering_team.shared.v2_review import LlmReviewOutput

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
    from software_engineering_team.shared.v2_review import LlmReviewOutput

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


# ---------------------------------------------------------------------------
# _code_review_step / CodeReviewInput files=<surface.blocks> + pre_numbered= surface wiring
# ---------------------------------------------------------------------------


def test_run_review_old_contents_meaningful_diff_uses_surface_code(tmp_path: Path) -> None:
    """When ``old_contents`` yields a meaningful, purely-additive diff (no
    deleted line -- see ``_patch_has_any_removal``), the external agent's
    ``CodeReviewInput`` carries ``files=<surface.blocks>``/``pre_numbered=True``
    instead of the whole-file ``files=files`` mapping."""
    config = _build_config()
    old_contents = {"a.py": "def f():\n    return 1\n"}
    files = {"a.py": old_contents["a.py"] + "\n\ndef g():\n    return 2\n"}

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result(files),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: [],
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
        old_contents=old_contents,
    )

    cr_input = cr_agent.run.call_args.args[0]
    expected_surface = _maybe_build_change_surface_from_pairs(files, old_contents)
    assert expected_surface is not None
    assert cr_input.files == dict(expected_surface.blocks)
    assert cr_input.pre_numbered is True


def test_run_review_old_contents_none_default_keeps_files_behavior(tmp_path: Path) -> None:
    """``old_contents`` defaults to ``None`` -- every existing caller that does not
    pass it keeps today's ``files=`` construction unchanged (no surface attempted)."""
    config = _build_config()
    files = {"a.py": "def f():\n    return 1\n"}

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result(files),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: [],
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
    )

    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.files == files
    assert cr_input.pre_numbered is False


def test_run_review_old_contents_identical_to_files_keeps_files_behavior(tmp_path: Path) -> None:
    """``old_contents`` supplied but identical to ``files`` (no meaningful diff) still
    falls back to ``files=`` -- an empty/no-op diff never masquerades as a surface."""
    config = _build_config()
    files = {"a.py": "unchanged\n"}

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result(files),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: [],
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
        old_contents=dict(files),
    )

    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.files == files
    assert cr_input.pre_numbered is False


def test_microtask_old_contents_meaningful_diff_uses_surface_code(tmp_path: Path) -> None:
    """``run_microtask_review`` threads ``old_contents`` through to the same
    ``_code_review_step`` surface wiring as ``run_review``. Purely additive (no
    deleted line -- see ``_patch_has_any_removal``), same rationale as
    ``test_run_review_old_contents_meaningful_diff_uses_surface_code``."""
    config = _build_config()
    old_contents = {"a.py": "def f():\n    return 1\n"}
    files = {"a.py": old_contents["a.py"] + "\n\ndef g():\n    return 2\n"}

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files=files,
        code_review_agent=cr_agent,
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: [],
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
        old_contents=old_contents,
    )

    cr_input = cr_agent.run.call_args.args[0]
    expected_surface = _maybe_build_change_surface_from_pairs(files, old_contents)
    assert expected_surface is not None
    assert cr_input.files == dict(expected_surface.blocks)
    assert cr_input.pre_numbered is True


def test_microtask_old_contents_none_default_keeps_files_behavior(tmp_path: Path) -> None:
    """``run_microtask_review`` without ``old_contents`` keeps today's ``files=``
    construction unchanged."""
    config = _build_config()
    files = {"a.py": "def f():\n    return 1\n"}

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files=files,
        code_review_agent=cr_agent,
        language="python",
        llm_review_fn=lambda *, llm, task, files, **kw: [],
        qa_agent_fn=lambda **kw: [],
        security_agent_fn=lambda **kw: [],
        build_verify_fn=_build_verify_fn,
    )

    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.files == files
    assert cr_input.pre_numbered is False


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
    assert "a.py" in result.blocks


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


# ---------------------------------------------------------------------------
# _resolve_change_surface_for_review / _code_review_step surface-vs-fallback
# wiring (no-base and empty-diff fallback triggers, GitHub issue #5400)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(repo_path: Path) -> None:
    """Initialize ``repo_path`` in place as a git repo (mirrors
    ``test_previous_content_git.py``'s ``_init_repo``, but initializes the
    caller's own directory rather than creating a ``repo`` subdirectory, so
    the same ``tmp_path`` doubles as both ``repo_path`` and the git root)."""
    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "test@test.com")
    _git(repo_path, "config", "user.name", "Test")
    _git(repo_path, "config", "commit.gpgsign", "false")


def _commit_file(repo_path: Path, rel_path: str, content: str) -> None:
    target = repo_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo_path, "add", rel_path)
    _git(repo_path, "commit", "-m", f"add {rel_path}")


def test_code_review_no_git_repo_falls_back_to_files(tmp_path: Path) -> None:
    """No ``.git`` under ``repo_path`` -> no base resolves for any path -> the
    external agent still runs, submitted ``files`` as-is (unchanged from
    pre-#5400 behavior). Also pins that every existing test using a plain
    ``tmp_path`` (no git) keeps getting the same ``files=`` submission."""
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
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.files == {"x.py": "code"}
    assert cr_input.pre_numbered is False


def test_run_review_no_git_repo_falls_back_to_files(tmp_path: Path) -> None:
    """``run_review`` (execution_result path) with no ``.git`` under
    ``repo_path``: no base resolves, so the fallback still runs the
    external agent, submitted ``files`` as-is rather than being silently
    skipped. Mirrors ``test_code_review_no_git_repo_falls_back_to_files``,
    which covers the same no-base fallback through ``run_microtask_review``."""
    config = _build_config()
    files = {"x.py": "code"}
    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result(files),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.files == files
    assert cr_input.pre_numbered is False


def test_run_review_no_git_repo_preserves_reader_language_and_context(
    tmp_path: Path,
) -> None:
    """Combines what the narrower tests above pin individually: with no ``.git``
    under ``repo_path`` (no base resolves, so the external agent's
    ``CodeReviewInput`` still falls back to ``files=`` as-is), the *same* call
    also carries a real, working ``DiskRepoReader`` rooted at ``repo_path`` --
    not a monkeypatched stand-in -- plus the caller's ``language`` and
    ``review_context`` (architecture + spec_content), all in one no-base
    round trip through ``run_review``'s external-agent path."""
    config = _build_config()
    (tmp_path / "x.py").write_text("code")
    files = {"x.py": "code"}
    ctx = ReviewContext(architecture=SystemArchitecture(overview="layered"), spec_content="spec")
    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result(files),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
        language="python",
        review_context=ctx,
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    # Fallback shape: no base resolved, so `files=` is submitted as-is.
    assert cr_input.files == files
    assert cr_input.pre_numbered is False
    # language / review_context preserved onto the same CodeReviewInput.
    assert cr_input.language == "python"
    assert cr_input.architecture is ctx.architecture
    assert cr_input.spec_content == "spec"
    # A real DiskRepoReader, rooted at repo_path, actually reads back the file.
    reader = cr_agent.run.call_args.kwargs["repo_reader"]
    assert isinstance(reader, DiskRepoReader)
    assert reader.read_file("x.py") == "code"


def test_code_review_empty_diff_falls_back_to_files(tmp_path: Path) -> None:
    """A real git base exists, but it's identical to the new content -> no
    surface is built (nothing changed); the agent still runs, submitted
    ``files`` as-is rather than a degenerate empty surface."""
    config = _build_config()
    _init_repo(tmp_path)
    _commit_file(tmp_path, "x.py", "code")

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
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.files == {"x.py": "code"}
    assert cr_input.pre_numbered is False


def test_code_review_purely_additive_diff_uses_change_surface(tmp_path: Path) -> None:
    """A real git base exists and the new content only APPENDS new lines --
    no existing line is touched or removed -- so the agent is submitted the
    diff-derived change surface (``files=<surface.blocks>``, ``pre_numbered=True``)
    instead of the whole-file ``files=files`` mapping, with ``full_content``
    (scoped to the surface's own paths)
    riding along so the coordinator's whole-codebase side-effect/architecture
    passes still see real full bodies instead of being disabled by
    ``pre_numbered=True``. Any deletion at all (a replaced or removed line)
    instead falls back to ``files=`` -- see
    ``test_code_review_same_line_edit_falls_back_to_files`` -- since the
    change surface can never prove a removed line's information survives
    elsewhere in the rendered surface."""
    config = _build_config()
    _init_repo(tmp_path)
    old_content = "def existing():\n    return 1\n"
    _commit_file(tmp_path, "x.py", old_content)
    files = {"x.py": old_content + "\n\ndef added():\n    return 2\n"}

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files=files,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    expected_surface = _resolve_change_surface_for_review(files, tmp_path)
    assert expected_surface is not None
    assert cr_input.files == dict(expected_surface.blocks)
    assert cr_input.pre_numbered is True
    assert "def added" in cr_input.files["x.py"]
    assert cr_input.full_content == files


def test_run_review_git_auto_resolved_base_uses_change_surface(tmp_path: Path) -> None:
    """``run_review`` (execution_result path) with no caller-supplied
    ``old_contents``: a real git base exists under ``repo_path`` and the new
    content only appends -- the base is auto-resolved from ``HEAD`` (see
    ``_resolve_change_surface_for_review``) and the external agent is
    submitted the diff-derived change surface (``files=<surface.blocks>``,
    ``pre_numbered=True``) rather than the whole-file ``files=files`` mapping.
    Mirrors
    ``test_code_review_purely_additive_diff_uses_change_surface``, which
    covers the same auto-resolve path through ``run_microtask_review``."""
    config = _build_config()
    _init_repo(tmp_path)
    old_content = "def existing():\n    return 1\n"
    _commit_file(tmp_path, "x.py", old_content)
    files = {"x.py": old_content + "\n\ndef added():\n    return 2\n"}

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        execution_result=_execution_result(files),
        repo_path=tmp_path,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    expected_surface = _resolve_change_surface_for_review(files, tmp_path)
    assert expected_surface is not None
    assert cr_input.files == dict(expected_surface.blocks)
    assert cr_input.pre_numbered is True
    assert "def added" in cr_input.files["x.py"]
    assert cr_input.full_content == files


def test_code_review_same_line_edit_falls_back_to_files(tmp_path: Path) -> None:
    """A real git base exists but the new content replaces an existing line
    (not a pure append) -- even a single-line, same-function edit -- so the
    diff contains a deletion the surface can never prove is safely
    represented (see ``_patch_has_any_removal``); the agent must be
    submitted ``files=`` as-is rather than a surface."""
    config = _build_config()
    _init_repo(tmp_path)
    _commit_file(tmp_path, "x.py", "def f():\n    return 0\n")
    files = {"x.py": "def f():\n    return 1\n"}

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files=files,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.files == files
    assert cr_input.pre_numbered is False


def test_resolve_change_surface_for_review_no_files_returns_none(tmp_path: Path) -> None:
    assert _resolve_change_surface_for_review({}, tmp_path) is None


def test_resolve_change_surface_for_review_new_file_no_base_returns_none(
    tmp_path: Path,
) -> None:
    """A real git repo exists (HEAD is resolvable), but the given path was
    never committed -> a pure miss for every path -> no base at all, so the
    caller must fall back to ``files=`` rather than treating the file as a
    from-scratch surface."""
    _init_repo(tmp_path)
    _commit_file(tmp_path, "unrelated.py", "unrelated\n")

    result = _resolve_change_surface_for_review({"new.py": "brand new\n"}, tmp_path)

    assert result is None


def test_resolve_change_surface_for_review_deletion_only_file_returns_none(
    tmp_path: Path,
) -> None:
    """A deletion-only diff (old had a function, new drops it with no added
    lines) contributes no touched lines, so the builder silently omits that
    path from ``blocks`` even though it genuinely changed. When another file
    in the same submission DOES have added lines, the builder still returns a
    non-empty (but partial) surface -- that partial surface must be rejected
    (``None``) rather than silently submitted, or the reviewer would approve
    without ever seeing the dropped file's change."""
    _init_repo(tmp_path)
    _commit_file(
        tmp_path,
        "a.py",
        "def validate(x):\n    if not x:\n        raise ValueError('bad')\n    return x\n\n\ndef other():\n    return 1\n",
    )
    _commit_file(tmp_path, "b.py", "def helper():\n    return 1\n")

    files = {
        # validate() deleted entirely -> pure removal, no added touched lines.
        "a.py": "def other():\n    return 1\n",
        # b.py gains a function -> has added touched lines.
        "b.py": "def helper():\n    return 1\n\n\ndef added():\n    return 2\n",
    }

    result = _resolve_change_surface_for_review(files, tmp_path)

    assert result is None


def test_code_review_deletion_only_file_falls_back_to_files(tmp_path: Path) -> None:
    """End-to-end: a mixed submission where one file's only change is a
    deletion must submit ``files=`` (every path), never a surface that omits
    the deletion."""
    config = _build_config()
    _init_repo(tmp_path)
    _commit_file(
        tmp_path,
        "a.py",
        "def validate(x):\n    if not x:\n        raise ValueError('bad')\n    return x\n\n\ndef other():\n    return 1\n",
    )
    _commit_file(tmp_path, "b.py", "def helper():\n    return 1\n")

    files = {
        "a.py": "def other():\n    return 1\n",
        "b.py": "def helper():\n    return 1\n\n\ndef added():\n    return 2\n",
    }

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files=files,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.files == files
    assert cr_input.pre_numbered is False


# ---------------------------------------------------------------------------
# _patch_has_any_removal
# ---------------------------------------------------------------------------


def test_patch_has_any_removal_true_for_pure_deletion_hunk() -> None:
    patch = "--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,0 @@\n-def validate():\n-    pass\n-\n"
    assert _patch_has_any_removal(patch) is True


def test_patch_has_any_removal_false_for_pure_addition_hunk() -> None:
    patch = "--- a/a.py\n+++ b/a.py\n@@ -1,0 +1,2 @@\n+def added():\n+    return 1\n"
    assert _patch_has_any_removal(patch) is False


def test_patch_has_any_removal_true_for_same_spot_modify_hunk() -> None:
    """Even a same-spot modification (a removed line immediately followed by
    an added replacement) counts -- there is no general, language-agnostic
    way to prove the removed line's information survives elsewhere in the
    rendered surface, so any deletion at all is treated as unrepresented."""
    patch = "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-def old_name():\n+def new_name():\n     return 1\n"
    assert _patch_has_any_removal(patch) is True


def test_patch_has_any_removal_true_when_only_one_of_several_hunks_has_a_deletion() -> None:
    """A deletion buried in a later hunk, behind an earlier addition-only
    hunk, must still be found."""
    patch = (
        "--- a/a.py\n+++ b/a.py\n"
        "@@ -1,0 +1,2 @@\n+def added():\n+    return 1\n"
        "@@ -10,2 +12,0 @@\n-def removed():\n-    pass\n"
    )
    assert _patch_has_any_removal(patch) is True


def test_patch_has_any_removal_blank_patch_returns_false() -> None:
    assert _patch_has_any_removal("") is False


def test_patch_has_any_removal_no_hunks_returns_false() -> None:
    assert _patch_has_any_removal("--- a/a.py\n+++ b/a.py\n") is False


# ---------------------------------------------------------------------------
# Mixed hunk with an unrepresented deletion, and a new blank-content path
# treated as unchanged -- GitHub issue #5400 follow-up (round 2)
# ---------------------------------------------------------------------------

# validate() is deleted immediately before other(), and other()'s body is
# also modified -- close enough that difflib merges the deletion and the
# modification into ONE hunk. Any deletion at all now falls back (see
# _patch_has_any_removal), so this is caught regardless of hunk merging.
_MIXED_SINGLE_HUNK_OLD = (
    "def validate(x):\n"
    "    if not x:\n"
    "        raise ValueError('bad')\n"
    "    return x\n"
    "\n"
    "def other():\n"
    "    return 1\n"
)
_MIXED_SINGLE_HUNK_NEW = "def other():\n    return 2\n"


def test_resolve_change_surface_for_review_mixed_hunk_unrepresented_deletion_returns_none(
    tmp_path: Path,
) -> None:
    """A path present in ``surface.blocks`` (its hunk has an added line) whose
    same hunk also deletes an unrelated function must not be treated as
    covered."""
    _init_repo(tmp_path)
    _commit_file(tmp_path, "a.py", _MIXED_SINGLE_HUNK_OLD)

    result = _resolve_change_surface_for_review({"a.py": _MIXED_SINGLE_HUNK_NEW}, tmp_path)

    assert result is None


def test_code_review_mixed_hunk_unrepresented_deletion_falls_back_to_files(
    tmp_path: Path,
) -> None:
    """End-to-end: the same mixed-single-hunk scenario must submit ``files=``
    rather than a surface whose rendered body omits the deletion."""
    config = _build_config()
    _init_repo(tmp_path)
    _commit_file(tmp_path, "a.py", _MIXED_SINGLE_HUNK_OLD)
    files = {"a.py": _MIXED_SINGLE_HUNK_NEW}

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files=files,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.files == files
    assert cr_input.pre_numbered is False


# ---------------------------------------------------------------------------
# Same-spot construct swap (deletion+addition pair whose deleted symbol's
# identity never reappears) -- GitHub issue #5400 follow-up (round 3);
# subsumed by _patch_has_any_removal's blanket "any deletion" rule (round 4),
# kept as regression coverage for this specific real-world scenario.
# ---------------------------------------------------------------------------

_CONSTRUCT_SWAP_OLD = (
    "def validate(x):\n"
    "    if not x:\n"
    "        raise ValueError('bad')\n"
    "    return x\n"
    "\n"
    "def other():\n"
    "    return validate(1)\n"
)
_CONSTRUCT_SWAP_NEW = "def unrelated():\n    return 1\n\ndef other():\n    return validate(1)\n"


def test_resolve_change_surface_for_review_construct_swap_returns_none(
    tmp_path: Path,
) -> None:
    """A deleted function (``validate``) immediately replaced by an unrelated one
    (``unrelated``) is a same-spot deletion+addition pair -- the deleted
    symbol, still called by the unchanged ``other()``, would be invisible in
    the rendered surface. Must fall back rather than hide it."""
    _init_repo(tmp_path)
    _commit_file(tmp_path, "a.py", _CONSTRUCT_SWAP_OLD)

    result = _resolve_change_surface_for_review({"a.py": _CONSTRUCT_SWAP_NEW}, tmp_path)

    assert result is None


def test_code_review_construct_swap_falls_back_to_files(tmp_path: Path) -> None:
    """End-to-end: the same construct-swap scenario must submit ``files=``
    rather than a surface that omits the removed function's identity."""
    config = _build_config()
    _init_repo(tmp_path)
    _commit_file(tmp_path, "a.py", _CONSTRUCT_SWAP_OLD)
    files = {"a.py": _CONSTRUCT_SWAP_NEW}

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files=files,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.files == files
    assert cr_input.pre_numbered is False


_BLANK_PATH_SCENARIO_OLD_A = "def f():\n    return 1\n"
# Purely additive (no deleted line) so this isolates the blank-path gap --
# without this, _patch_has_any_removal would already reject a.py's own diff
# for an unrelated reason, and the test would pass without actually
# exercising the blank-path coverage check at all.
_BLANK_PATH_SCENARIO_NEW_A = _BLANK_PATH_SCENARIO_OLD_A + "\n\ndef g():\n    return 2\n"


def test_resolve_change_surface_for_review_new_blank_path_not_treated_as_unchanged(
    tmp_path: Path,
) -> None:
    """A brand-new path (never in the resolved base) whose new content happens
    to be blank -- e.g. a newly added ``.gitkeep`` marker -- must not be
    silently treated as "unchanged": it can never appear in the built
    surface (blank content is always omitted), so its presence must force
    the fallback rather than being skipped by an ``old.get(path, "") ==
    new_text`` coincidence. ``a.py``'s own change is purely additive (no
    deletion) so it alone would otherwise produce a valid surface -- isolating
    the blank-path gap as the sole reason for the fallback."""
    _init_repo(tmp_path)
    _commit_file(tmp_path, "a.py", _BLANK_PATH_SCENARIO_OLD_A)
    files = {
        "a.py": _BLANK_PATH_SCENARIO_NEW_A,
        ".gitkeep": "",
    }

    result = _resolve_change_surface_for_review(files, tmp_path)

    assert result is None


def test_code_review_new_blank_path_falls_back_to_files(tmp_path: Path) -> None:
    """End-to-end: a new blank-content path alongside a real, purely-additive
    change must submit ``files=`` rather than a surface that silently
    excludes it."""
    config = _build_config()
    _init_repo(tmp_path)
    _commit_file(tmp_path, "a.py", _BLANK_PATH_SCENARIO_OLD_A)
    files = {
        "a.py": _BLANK_PATH_SCENARIO_NEW_A,
        ".gitkeep": "",
    }

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files=files,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.files == files
    assert cr_input.pre_numbered is False


def test_code_review_full_content_scoped_to_surface_paths_not_all_files(
    tmp_path: Path,
) -> None:
    """When ``files`` includes a path this task left byte-identical to
    ``HEAD`` alongside a genuinely (purely-additively) changed one, the
    identical path is correctly omitted from ``surface.blocks`` -- and
    ``full_content`` on the built ``CodeReviewInput`` must match that same
    scope, not include the unchanged path, or the coordinator's whole-codebase
    passes would analyze code this task never touched as if it had."""
    config = _build_config()
    _init_repo(tmp_path)
    _commit_file(tmp_path, "changed.py", "def f():\n    return 1\n")
    _commit_file(tmp_path, "untouched.py", "def g():\n    return 2\n")
    files = {
        "changed.py": "def f():\n    return 1\n\n\ndef added():\n    return 3\n",
        "untouched.py": "def g():\n    return 2\n",  # byte-identical to HEAD
    }

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files=files,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.pre_numbered is True
    assert cr_input.full_content == {"changed.py": files["changed.py"]}


# ---------------------------------------------------------------------------
# Within-file removal-only hunk (a covered path whose rendered block still
# omits a distant deletion), GitHub issue #5400 follow-up
# ---------------------------------------------------------------------------

# Old/new content for a single file with two well-separated hunks: an
# early pure-deletion hunk (removes validate()) and a later pure-addition
# hunk (adds added()). Difflib keeps these as two distinct hunks because the
# filler lines between them exceed the default 3-line context window.
_MIXED_HUNK_OLD = (
    "def validate(x):\n"
    "    if not x:\n"
    "        raise ValueError('bad')\n"
    "    return x\n"
    "\n"
    "\n"
    "def filler1(): return 1\n"
    "def filler2(): return 2\n"
    "def filler3(): return 3\n"
    "def filler4(): return 4\n"
    "def filler5(): return 5\n"
    "def filler6(): return 6\n"
    "def filler7(): return 7\n"
    "def filler8(): return 8\n"
    "\n"
    "def other():\n"
    "    return 1\n"
    "\n"
)
_MIXED_HUNK_NEW = (
    "def filler1(): return 1\n"
    "def filler2(): return 2\n"
    "def filler3(): return 3\n"
    "def filler4(): return 4\n"
    "def filler5(): return 5\n"
    "def filler6(): return 6\n"
    "def filler7(): return 7\n"
    "def filler8(): return 8\n"
    "\n"
    "def other():\n"
    "    return 1\n"
    "\n"
    "def added():\n"
    "    return 2\n"
    "\n"
)


def test_resolve_change_surface_for_review_within_file_removal_only_hunk_returns_none(
    tmp_path: Path,
) -> None:
    """A path present in ``surface.blocks`` (it has an added-only hunk) but
    whose distant removal-only hunk (a deleted function) is nowhere in the
    rendered body must not be treated as covered."""
    _init_repo(tmp_path)
    _commit_file(tmp_path, "a.py", _MIXED_HUNK_OLD)

    result = _resolve_change_surface_for_review({"a.py": _MIXED_HUNK_NEW}, tmp_path)

    assert result is None


def test_code_review_within_file_removal_only_hunk_falls_back_to_files(
    tmp_path: Path,
) -> None:
    """End-to-end: the same within-file scenario must submit ``files=``
    rather than a surface whose rendered body omits the deletion."""
    config = _build_config()
    _init_repo(tmp_path)
    _commit_file(tmp_path, "a.py", _MIXED_HUNK_OLD)
    files = {"a.py": _MIXED_HUNK_NEW}

    cr_agent = MagicMock()
    cr_agent.run.return_value = MagicMock(issues=[])

    run_microtask_review(
        config=config,
        llm=DummyLLMClient(),
        task=_task(),
        microtask=_microtask(),
        repo_path=tmp_path,
        files=files,
        code_review_agent=cr_agent,
        language="python",
        **_noop_runners(),
    )

    assert cr_agent.run.called
    cr_input = cr_agent.run.call_args.args[0]
    assert cr_input.files == files
    assert cr_input.pre_numbered is False
